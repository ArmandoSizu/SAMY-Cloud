"""Emparejamiento entre el catalogo comercial y el catalogo del proveedor.

LA REGLA
--------

    UN MAPPING POR PRECIO NO ES UN MAPPING.

Es la trampa central de este dominio y merece decirse con un ejemplo real:
Telcel vende, con el mismo nombre de marca y el mismo precio de $100, cosas
distintas segun sea *recarga de saldo* o *paquete Amigo Sin Limite* (ver
``docs/catalogo-recargas-mexico.md``). Un emparejador que vea "$100 = $100"
manda al cliente el producto equivocado y le cobra el correcto. El cliente
reclama, el dinero ya se movio, y el saldo del proveedor ya se gasto.

Por eso una coincidencia exige LAS CUATRO partes de la identidad:

    operador + familia + codigo/SKU + importe

y las tres primeras se comparan por nombre, no por parecido. Si el proveedor
llama distinto a una familia, hace falta un alias que **escribe una persona**
(``CommercialFamily.provider_aliases``). Sin alias no se adivina: se reporta
``FAMILIA_NO_RECONOCIDA`` y alguien decide.

LO QUE ESTE MODULO NO HACE
--------------------------

* **No aprueba nada.** Todo mapping que crea nace en ``REVIEW_REQUIRED`` y
  ``enabled=False``. Aprobar es un acto humano, en el panel de plataforma.
* **No empareja por similitud de texto.** No hay distancia de edicion ni
  "contiene". Dos nombres coinciden o no coinciden.
* **No inventa importes.** Un producto del proveedor sin importe declarado
  solo puede emparejar con monto libre, jamas con una denominacion fija.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import structlog

from apps.commercial.models import (
    CommercialProduct,
    Environment,
    MappingStatus,
    ProviderCatalogItem,
    ProviderProductMapping,
)

log = structlog.get_logger("commercial.mapping")


class Rechazo(enum.StrEnum):
    """Por que no se emparejo. Se reporta tal cual a quien revisa."""

    SIN_CATALOGO = "SIN_CATALOGO"
    OPERADOR_NO_RECONOCIDO = "OPERADOR_NO_RECONOCIDO"
    FAMILIA_NO_RECONOCIDA = "FAMILIA_NO_RECONOCIDA"
    IMPORTE_NO_COINCIDE = "IMPORTE_NO_COINCIDE"
    SIN_SKU = "SIN_SKU"
    #: Coincidio el importe y nada mas. Es el caso que existe para NO aceptarse.
    SOLO_PRECIO = "SOLO_PRECIO"
    AMBIGUO = "AMBIGUO"


@dataclass(frozen=True, slots=True)
class Propuesta:
    """Resultado de emparejar UN producto comercial contra UN proveedor."""

    producto: CommercialProduct
    provider_slug: str
    environment: str
    #: El unico candidato que cumplio las cuatro. ``None`` si no hubo.
    elegido: ProviderCatalogItem | None = None
    motivo: Rechazo | None = None
    detalle: str = ""
    #: Candidatos que coincidieron SOLO en precio. Se reportan para que se vea
    #: lo que se descarto, no para ofrecerlos como alternativa aceptable.
    descartados_por_precio: tuple[ProviderCatalogItem, ...] = field(
        default_factory=tuple
    )

    @property
    def hay_coincidencia(self) -> bool:
        return self.elegido is not None


def _normalizar(texto: str) -> str:
    return (texto or "").strip().upper()


def emparejar(
    producto: CommercialProduct,
    *,
    provider_slug: str,
    environment: str,
) -> Propuesta:
    """Busca el SKU del proveedor que corresponde a este producto comercial.

    Devuelve una ``Propuesta``, nunca crea nada en la base. Separar el calculo
    de la escritura permite correr el emparejador en seco y mirar el resultado
    antes de tocar el catalogo.
    """
    candidatos = list(
        ProviderCatalogItem.objects.filter(
            provider_slug=provider_slug, environment=environment, active=True
        )
    )
    if not candidatos:
        return Propuesta(
            producto=producto,
            provider_slug=provider_slug,
            environment=environment,
            motivo=Rechazo.SIN_CATALOGO,
            detalle=(
                f"No hay catalogo importado de '{provider_slug}' en "
                f"{environment}. Importalo antes de emparejar."
            ),
        )

    alias_operador = producto.operator.alias_de(provider_slug)
    alias_familia = producto.family.alias_de(provider_slug)

    # Se filtra en tres pasos y se guarda el resultado de cada uno. Asi el
    # rechazo dice EN QUE PASO se cayo, que es la diferencia entre "arregla el
    # alias del operador" y "arregla el alias de la familia".
    por_operador = [
        c for c in candidatos if _normalizar(c.provider_operator) in alias_operador
    ]
    por_familia = [
        c for c in por_operador if _normalizar(c.provider_family) in alias_familia
    ]
    exactos = [
        c
        for c in por_familia
        if c.amount_cents == producto.price_cents and c.provider_product_id
    ]

    # Lo que coincide en precio y NADA mas. Se calcula siempre, aunque haya
    # coincidencia exacta, porque verlo es lo que le ensena a quien revisa que
    # emparejar por precio habria elegido otra cosa.
    solo_precio = tuple(
        c
        for c in candidatos
        if c.amount_cents == producto.price_cents and c not in por_familia
    )

    if len(exactos) == 1:
        return Propuesta(
            producto=producto,
            provider_slug=provider_slug,
            environment=environment,
            elegido=exactos[0],
            descartados_por_precio=solo_precio,
        )

    if len(exactos) > 1:
        # Dos SKUs del proveedor con la misma identidad completa. No se elige
        # "el primero": elegir al azar entre dos productos indistinguibles es
        # exactamente el error que este modulo evita.
        return Propuesta(
            producto=producto,
            provider_slug=provider_slug,
            environment=environment,
            motivo=Rechazo.AMBIGUO,
            detalle=(
                f"{len(exactos)} productos de '{provider_slug}' tienen la misma "
                "identidad: "
                + ", ".join(sorted(c.provider_product_id for c in exactos))
                + ". Tiene que decidirlo una persona."
            ),
            descartados_por_precio=solo_precio,
        )

    return _explicar_fallo(
        producto,
        provider_slug=provider_slug,
        environment=environment,
        por_operador=por_operador,
        por_familia=por_familia,
        alias_operador=alias_operador,
        alias_familia=alias_familia,
        solo_precio=solo_precio,
    )


def _explicar_fallo(
    producto: CommercialProduct,
    *,
    provider_slug: str,
    environment: str,
    por_operador: list[ProviderCatalogItem],
    por_familia: list[ProviderCatalogItem],
    alias_operador: tuple[str, ...],
    alias_familia: tuple[str, ...],
    solo_precio: tuple[ProviderCatalogItem, ...],
) -> Propuesta:
    """Dice en que paso se cayo y que hay que hacer para arreglarlo.

    Un "no se pudo emparejar" a secas obliga a quien revisa a reconstruir el
    razonamiento a mano sobre cientos de filas. El motivo concreto es la
    diferencia entre cinco minutos y una tarde.
    """

    def armar(motivo: Rechazo, detalle: str) -> Propuesta:
        return Propuesta(
            producto=producto,
            provider_slug=provider_slug,
            environment=environment,
            motivo=motivo,
            detalle=detalle,
            descartados_por_precio=solo_precio,
        )

    if not por_operador:
        return armar(
            Rechazo.OPERADOR_NO_RECONOCIDO,
            (
                f"Ningun producto de '{provider_slug}' declara el operador "
                f"{producto.operator.code}. Alias configurados: "
                f"{', '.join(alias_operador)}. Agrega el nombre que usa el "
                f"proveedor en CommercialOperator.provider_aliases['{provider_slug}']."
            ),
        )

    if not por_familia:
        vistas = sorted({_normalizar(c.provider_family) for c in por_operador if c.provider_family})
        return armar(
            Rechazo.FAMILIA_NO_RECONOCIDA,
            (
                f"El proveedor tiene productos de {producto.operator.code} pero "
                f"ninguno en la familia {producto.family.code}. Alias "
                f"configurados: {', '.join(alias_familia)}. Familias que el "
                f"proveedor si declara: {', '.join(vistas) or 'ninguna'}."
            ),
        )

    sin_sku = [c for c in por_familia if not c.provider_product_id]
    if sin_sku and len(sin_sku) == len(por_familia):
        return armar(
            Rechazo.SIN_SKU,
            "Los productos que coinciden no traen codigo/SKU: no se pueden ejecutar.",
        )

    importes = sorted({c.amount_cents for c in por_familia if c.amount_cents is not None})
    return armar(
        Rechazo.IMPORTE_NO_COINCIDE,
        (
            f"Coinciden operador y familia, pero ninguno vale "
            f"{producto.price_cents} centavos. Importes que ofrece el proveedor "
            f"en esa familia: {importes or 'ninguno declarado'}."
        ),
    )


def guardar(propuesta: Propuesta, *, priority: int = 100) -> ProviderProductMapping | None:
    """Escribe la propuesta como mapping EN REVISION. Nunca habilitado.

    Es deliberado que esta funcion no pueda producir un mapping vendible. El
    emparejador acierta la mayoria de las veces y precisamente por eso no se
    le da la ultima palabra: la venta la autoriza una persona en el panel,
    comparando nuestra fila con la del proveedor.
    """
    if not propuesta.hay_coincidencia or propuesta.elegido is None:
        return None

    item = propuesta.elegido
    mapping, creado = ProviderProductMapping.objects.update_or_create(
        product=propuesta.producto,
        provider_slug=propuesta.provider_slug,
        environment=propuesta.environment,
        defaults={
            "provider_product_id": item.provider_product_id,
            "provider_family": item.provider_family,
            "provider_product_name": item.provider_product_name,
            "provider_amount_cents": item.amount_cents,
            "amount_in_sku": item.amount_in_sku,
            "provider_fingerprint": item.fingerprint,
            "last_verified_at": None,
            # Nace bloqueado. Las dos lineas siguientes son la razon de ser de
            # esta funcion: propone, no autoriza.
            "enabled": False,
            "status": MappingStatus.REVIEW_REQUIRED,
            "status_reason": (
                "Propuesto automaticamente por identidad exacta "
                "(operador + familia + SKU + importe). Falta revision humana."
            ),
            "priority": priority,
        },
    )
    log.info(
        "mapping_propuesto",
        producto=str(propuesta.producto.id),
        provider=propuesta.provider_slug,
        sku=item.provider_product_id,
        creado=creado,
    )
    return mapping


def emparejar_catalogo(
    *,
    provider_slug: str,
    environment: str,
    solo_operador: str = "",
) -> list[Propuesta]:
    """Empareja todo el catalogo comercial activo contra un proveedor."""
    productos = CommercialProduct.objects.filter(active=True).select_related(
        "operator", "family"
    )
    if solo_operador:
        productos = productos.filter(operator__code=solo_operador.upper())

    return [
        emparejar(p, provider_slug=provider_slug, environment=environment)
        for p in productos.order_by(
            "operator__display_priority", "family__display_priority", "price_cents"
        )
    ]


__all__ = [
    "Rechazo",
    "Propuesta",
    "emparejar",
    "emparejar_catalogo",
    "guardar",
    "Environment",
]
