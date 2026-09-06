"""La regla que decide si un producto se puede cobrar.

    OFICIAL != EJECUTABLE

Que Telcel publique oficialmente un paquete no significa que nosotros podamos
entregarlo. Las cuatro condiciones tienen que cumplirse a la vez:

    official_verified          el producto existe de verdad y sabemos donde lo leimos
  + mapping valido             algun proveedor sabe que identificador mandar
  + proveedor disponible       ese proveedor responde AHORA
  + ambiente correcto          el mapping es del ambiente en el que operamos
  = SELLABLE

POR QUE ESTO NO SE GUARDA EN LA BASE
-------------------------------------

Seria comodo tener una columna ``is_sellable`` e indexarla. Seria tambien una
mentira con fecha de caducidad: el dia que el proveedor se cae, la columna
sigue diciendo AVAILABLE y el cajero cobra algo que nadie va a entregar. Ese
es exactamente el fallo que el producto prohibe:

    "NUNCA: Cobrar -> despues descubrir que no podemos ejecutar."

Asi que se calcula, siempre, contra el estado real del momento. Para que eso
no cueste una llamada de salud por producto, la salud de los proveedores se
resuelve UNA vez por pantalla y se pasa como parametro.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from django.conf import settings
from django.db.models import Prefetch, QuerySet

from apps.commercial.models import (
    CatalogStatus,
    CommercialProduct,
    CommercialProductVersion,
    Environment,
    ProviderProductMapping,
)
from samy_common.providers.base import ProviderStatus

log = structlog.get_logger("commercial")


def ambiente_actual() -> str:
    """El ambiente en el que SAMY Cloud esta operando.

    Se deriva del modo del proveedor configurado, no de una variable propia:
    tener dos fuentes para lo mismo termina con una diciendo PRODUCTION
    mientras la otra apunta al sandbox.
    """
    slug = settings.TOPUP_PROVIDER
    crudo = getattr(settings, f"{slug.upper()}_MODE", "SANDBOX")
    return (
        Environment.PRODUCTION
        if str(crudo).strip().upper() == "PRODUCTION"
        else Environment.SANDBOX
    )


def proveedores_listos() -> frozenset[str]:
    """Slugs de los proveedores que pueden ejecutar una recarga ahora mismo.

    Solo ``READY`` cuenta. ``PENDING_CONTRACT`` (el caso de Taecel hoy),
    ``NOT_CONFIGURED`` y ``DEGRADED`` no: un proveedor a medias no entrega
    nada, y tratarlo como disponible es prometer lo que no se puede cumplir.

    Se consulta una vez y se reparte. Si la salud de un proveedor no se puede
    determinar, NO se asume que esta bien.
    """
    from apps.providers.registry import describe_all

    listos = set()
    for meta in describe_all():
        if str(meta.get("status", "")) == ProviderStatus.READY:
            listos.add(str(meta.get("slug", "")))
    return frozenset(listos)


@dataclass(frozen=True, slots=True)
class Disponibilidad:
    """Por que un producto se puede o no se puede vender.

    ``motivo`` es texto para una persona. El de administracion explica que
    falta; el de caja no menciona proveedores porque al cajero no le sirve
    saber que Taecel no tiene contrato: le sirve saber que hoy no lo venda.
    """

    estado: str
    motivo: str
    mapping: ProviderProductMapping | None = None

    @property
    def vendible(self) -> bool:
        return self.estado == CatalogStatus.AVAILABLE


#: Lo que ve el cajero cuando algo no se puede vender. Uno solo, a proposito:
#: distinguir "sin proveedor" de "proveedor caido" es informacion de
#: administracion, y en el mostrador solo estorba.
MENSAJE_CAJA = "Temporalmente no disponible"


def disponibilidad(
    producto: CommercialProduct,
    *,
    listos: frozenset[str] | None = None,
    ambiente: str | None = None,
) -> Disponibilidad:
    """Resuelve el estado real de un producto. Es la unica puerta a la venta.

    El orden de las comprobaciones importa: primero lo que decide una persona
    (apagado, vencido, en revision) y despues lo que decide la infraestructura.
    Asi el motivo que se muestra es el mas util, y no "proveedor fuera de
    linea" cuando en realidad alguien lo apago a mano.
    """
    listos = proveedores_listos() if listos is None else listos
    ambiente = ambiente_actual() if ambiente is None else ambiente

    # --- 1. Lo que decidio una persona ------------------------------------
    if not producto.active:
        return Disponibilidad(CatalogStatus.DISABLED, "El producto esta apagado.")

    if producto.status == CatalogStatus.DISABLED:
        return Disponibilidad(
            CatalogStatus.DISABLED,
            producto.status_reason or "El producto esta apagado.",
        )

    if producto.status == CatalogStatus.REVIEW_REQUIRED:
        return Disponibilidad(
            CatalogStatus.REVIEW_REQUIRED,
            producto.status_reason or "Requiere revision antes de volver a venderse.",
        )

    if producto.status == CatalogStatus.SANDBOX_ONLY:
        # Producto tecnico. Existe para probar la tuberia, no para vender.
        return Disponibilidad(
            CatalogStatus.SANDBOX_ONLY,
            "Producto tecnico de pruebas. No se vende en caja.",
        )

    if producto.status == CatalogStatus.EXPIRED or producto.esta_vencido:
        return Disponibilidad(
            CatalogStatus.EXPIRED,
            producto.status_reason or "Se paso su vigencia comercial.",
        )

    # --- 2. Procedencia oficial -------------------------------------------
    if not producto.official_verified:
        return Disponibilidad(
            CatalogStatus.REVIEW_REQUIRED,
            "Sin verificar contra la fuente oficial del operador.",
        )

    # --- 3. Alguien tiene que saber ejecutarlo ----------------------------
    #
    # Aqui esta la diferencia entre oficial y ejecutable. El producto puede
    # ser perfectamente real y estar perfectamente documentado; si ninguna API
    # configurada sabe entregarlo, no se cobra.
    candidatos = [
        m
        for m in producto.mappings.all()
        if m.environment == ambiente and m.es_utilizable
    ]

    if not candidatos:
        return Disponibilidad(
            CatalogStatus.PROVIDER_NOT_MAPPED,
            f"Ningun proveedor tiene mapping utilizable en {ambiente}.",
        )

    # --- 4. Y tiene que estar de pie --------------------------------------
    operativos = [m for m in candidatos if m.provider_slug in listos]
    if not operativos:
        nombres = ", ".join(sorted({m.provider_slug for m in candidatos}))
        return Disponibilidad(
            CatalogStatus.PROVIDER_OFFLINE,
            f"Hay mapping ({nombres}) pero el proveedor no esta operativo.",
        )

    # Menor prioridad primero. El desempate por slug es solo para que el
    # resultado sea estable entre llamadas y las pruebas no dependan del orden
    # en que la base devuelva las filas.
    elegido = sorted(operativos, key=lambda m: (m.priority, m.provider_slug))[0]
    return Disponibilidad(CatalogStatus.AVAILABLE, "Disponible.", mapping=elegido)


def catalogo_comercial(
    *, ambiente: str | None = None, listos: frozenset[str] | None = None
) -> list[dict]:
    """El catalogo tal como lo ve el cajero.

    Devuelve TODOS los productos activos y no tecnicos, vendibles o no, porque
    esconder un producto que el operador si vende deja al cajero pensando que
    SAMY Cloud no lo tiene. Se muestra, y se marca por que hoy no se puede.

    Lo que NO sale de aqui: nada del proveedor. Ni slug, ni identificador, ni
    ambiente. Al cajero no le sirve y al cliente menos.
    """
    listos = proveedores_listos() if listos is None else listos
    ambiente = ambiente_actual() if ambiente is None else ambiente

    productos = (
        CommercialProduct.objects.filter(active=True)
        .exclude(status=CatalogStatus.SANDBOX_ONLY)
        .select_related("operator", "family")
        .prefetch_related(
            "mappings",
            Prefetch(
                "versions",
                queryset=CommercialProductVersion.objects.filter(is_current=True),
                to_attr="_vigentes",
            ),
        )
    )

    filas = []
    for producto in productos:
        estado = disponibilidad(producto, listos=listos, ambiente=ambiente)
        version = (producto._vigentes or [None])[0]  # type: ignore[attr-defined]
        filas.append(
            {
                "id": str(producto.id),
                "operator_code": producto.operator.code,
                "operator_name": producto.operator.name,
                "operator_priority": producto.operator.display_priority,
                "operator_is_primary": producto.operator.is_primary,
                "family_code": producto.family.code,
                "family_name": producto.family.name,
                "family_priority": producto.family.display_priority,
                "commercial_name": producto.commercial_name,
                "price_cents": producto.price_cents,
                "price_display": str(producto.price),
                "currency": producto.currency,
                "validity_days": version.validity_days if version else None,
                "data_mb": version.data_mb if version else None,
                "benefits": version.benefits if version else "",
                "calls": version.calls if version else "",
                "sms": version.sms if version else "",
                "sellable": estado.vendible,
                # El cajero ve un solo mensaje; el detalle es administrativo.
                "unavailable_reason": "" if estado.vendible else MENSAJE_CAJA,
            }
        )

    return filas


def productos_para_administracion(
    *, ambiente: str | None = None, listos: frozenset[str] | None = None
) -> QuerySet[CommercialProduct]:
    """Todo el catalogo, incluidos los tecnicos y los apagados.

    Administracion si necesita ver el detalle: fuente oficial, folio, fecha de
    verificacion, version, mapping, proveedor, ambiente y el motivo real del
    bloqueo. Es la contraparte de ``catalogo_comercial``.
    """
    return (
        CommercialProduct.objects.all()
        .select_related("operator", "family")
        .prefetch_related("mappings", "versions")
    )
