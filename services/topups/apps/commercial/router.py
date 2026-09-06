"""Eleccion de proveedor para un producto comercial.

El router traduce "Telcel Amigo Sin Limite $100" a "mandale este
identificador a este proveedor". Es la unica pieza que sabe que existe mas de
un proveedor; ni el catalogo comercial ni la ejecucion de la recarga lo saben.

POR QUE NO HAY FAILOVER AUTOMATICO
-----------------------------------

La tentacion es evidente: si el proveedor A falla, mandarselo al B y que el
cliente no se entere. Es la decision equivocada, y merece explicarse porque
parece lo contrario.

Un envio de recarga puede terminar de tres formas, no dos: exito, fallo, y
**no se sabe**. El tercero es el que importa. Un timeout no significa que la
recarga no se aplico; significa que no llego la respuesta. La recarga puede
estar perfectamente entregada del otro lado.

Si ante ese "no se sabe" el sistema reintenta con otro proveedor, el desenlace
mas probable no es "se salvo la venta": es **dos recargas al mismo telefono y
una sola cobrada**. El dinero de la segunda lo pone la tienda, y no se
recupera: el saldo ya esta en el telefono de un cliente que se fue.

Por eso aqui:

* El failover automatico entre proveedores **no existe**.
* Un fulfillment que ya tiene ``provider_slug`` solo puede volver a
  intentarse **con ese mismo proveedor**, y solo despues de que una consulta
  al proveedor haya resuelto sin ambiguedad que la anterior no se aplico.
* Cambiar de proveedor es una decision de una persona, no de un except.

La idempotencia vale mas que la disponibilidad. Una venta perdida se vuelve a
hacer; una recarga duplicada se paga.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from apps.commercial.models import CommercialProduct, ProviderProductMapping
from apps.commercial.services import Disponibilidad, disponibilidad
from apps.providers.base import TopupProvider
from apps.providers.registry import get_provider

log = structlog.get_logger("router")


class SinProveedorDisponible(Exception):
    """No hay forma de ejecutar este producto ahora mismo.

    Lleva el estado del catalogo para que quien la reciba pueda decir POR QUE
    no se puede, en vez de un "error" generico.
    """

    def __init__(self, estado: Disponibilidad) -> None:
        self.estado = estado
        super().__init__(estado.motivo)


class CambioDeProveedorProhibido(Exception):
    """Se intento reencaminar una recarga que ya salio hacia otro proveedor.

    Es un error de programacion, no una condicion de operacion: si esto se
    levanta, alguien escribio un failover.
    """


@dataclass(frozen=True, slots=True)
class Ruta:
    """Por donde se va a ejecutar una recarga."""

    mapping: ProviderProductMapping
    provider: TopupProvider

    @property
    def provider_slug(self) -> str:
        return self.mapping.provider_slug

    @property
    def provider_product_id(self) -> str:
        return self.mapping.provider_product_id


def elegir_ruta(
    producto: CommercialProduct,
    *,
    listos: frozenset[str] | None = None,
    ambiente: str | None = None,
) -> Ruta:
    """Elige por donde ejecutar un producto que todavia no se ha enviado.

    Reusa ``disponibilidad()`` en lugar de repetir la regla: si la venta y la
    ejecucion aplicaran criterios distintos, existiria un hueco por el que se
    cobra algo que despues no se puede mandar. Una sola definicion de
    "vendible", y esta es la que manda.
    """
    estado = disponibilidad(producto, listos=listos, ambiente=ambiente)

    if not estado.vendible or estado.mapping is None:
        log.info(
            "router_sin_proveedor",
            producto=str(producto.id),
            estado=estado.estado,
            motivo=estado.motivo,
        )
        raise SinProveedorDisponible(estado)

    return Ruta(mapping=estado.mapping, provider=get_provider(estado.mapping.provider_slug))


def ruta_para_reintento(
    producto: CommercialProduct, *, provider_slug_original: str, ambiente: str | None = None
) -> Ruta:
    """Ruta para reintentar una recarga que YA salio hacia un proveedor.

    Se exige el proveedor original y no se admite otro. Si su mapping ya no
    esta disponible, esto falla en vez de buscar alternativa: mandar la misma
    recarga a otro proveedor sin saber que paso con la primera es como se
    duplican las recargas.

    Esta funcion NO decide si conviene reintentar. Eso lo decide quien haya
    resuelto, con una consulta al proveedor, que la anterior no se aplico.
    """
    from apps.commercial.services import ambiente_actual

    ambiente = ambiente_actual() if ambiente is None else ambiente

    mapping = (
        producto.mappings.filter(
            provider_slug=provider_slug_original, environment=ambiente
        )
        .order_by("priority")
        .first()
    )

    if mapping is None or not mapping.es_utilizable:
        log.error(
            "router_reintento_sin_mapping",
            producto=str(producto.id),
            proveedor=provider_slug_original,
            ambiente=ambiente,
        )
        raise CambioDeProveedorProhibido(
            f"La recarga salio por '{provider_slug_original}' y ese proveedor ya no "
            f"tiene un mapping utilizable en {ambiente}. NO se reencamina a otro: "
            "hay que resolver a mano que paso con el envio anterior."
        )

    return Ruta(mapping=mapping, provider=get_provider(provider_slug_original))


def rutas_alternativas(
    producto: CommercialProduct, *, ambiente: str | None = None
) -> list[ProviderProductMapping]:
    """Otros proveedores que PODRIAN ejecutar este producto.

    Es informacion para administracion: sirve para ver la cobertura y para
    decidir a mano. **No la usa el flujo de ejecucion**, justamente para que
    no exista un camino por el que una recarga cambie de proveedor sola.
    """
    from apps.commercial.services import ambiente_actual

    ambiente = ambiente_actual() if ambiente is None else ambiente
    return list(
        producto.mappings.filter(environment=ambiente).order_by("priority", "provider_slug")
    )
