"""La guarda de saldo, en el punto donde todavia se puede decir que no.

DONDE
-----
En ``create_fulfillment``, que corre ANTES de que exista la orden. Sin
cumplimiento no hay orden, y sin orden no hay nada que cobrar. Ponerla en
``execute_topup`` la pondria despues del cobro, y entonces lo unico que
podria hacer es informar de un problema que ya costo dinero.

EL CACHE, Y POR QUE ES CORTO
----------------------------
Consultar el saldo en cada venta es una llamada de red por venta. Reloadly
castiga el exceso de llamadas **suspendiendo la cuenta**, y reactivarla exige
escribir a soporte: el cache no es una optimizacion, es evitar quedarse sin
proveedor a media jornada.

Y es corto porque un saldo cacheado puede estar viejo. Con una racha de
ventas seguidas el saldo real baja y el cacheado no, asi que el ultimo
minuto de ventas puede pasar la guarda con fondos que ya no existen. No se
puede evitar del todo sin llamar cada vez; se acota con dos cosas:

1. un TTL de segundos, no de minutos;
2. la reserva (``TOPUP_RESERVA_SALDO_CENTS``), un colchon que no se vende y
   que absorbe justo esa ventana.

La guarda es una barrera temprana, no un contador. La autoridad final sobre
el saldo sigue siendo el proveedor en el momento de la recarga.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from django.conf import settings
from django.core.cache import cache

from samy_common import saldo as reglas
from samy_common.money import Money
from samy_common.providers.base import ProviderMode
from samy_common.providers.exceptions import ProviderNotConfigured

if TYPE_CHECKING:  # pragma: no cover
    from apps.providers.base import TopupProvider

log = structlog.get_logger("fulfillment.saldo")

PREFIJO_CACHE: Final[str] = "samy:topups:saldo:"


class SaldoInsuficiente(ProviderNotConfigured):
    """No se puede vender: el proveedor no tiene (o no consta que tenga) fondos.

    Hereda de ``ProviderNotConfigured``, y no por pereza: ese es el unico
    error que TODO el camino ya sabe traducir. El manejador de excepciones de
    DRF le da su codigo de estado (ver ``samy_common.api.errors``) y el BFF lo
    convierte en la pantalla de "integracion no disponible" con su mensaje.
    Una clase hermana nueva llegaria al cajero como un 500, y un 500 no le
    dice "hoy no vendas esto", le dice que la aplicacion se rompio.

    Semanticamente no es "falta configuracion" sino "falta saldo", y el
    ``code`` propio permite distinguirlos en metricas. Lo que comparten es lo
    que importa aqui: el proveedor no puede atender esta operacion.

    Lleva los dos textos separados a proposito. ``motivo`` es para el panel y
    los registros; ``mensaje_caja`` es lo unico que ve el cajero, y no
    menciona proveedores ni saldos porque esa informacion no le sirve para
    atender al cliente que tiene delante.
    """

    code = "provider_insufficient_balance"

    def __init__(self, *, motivo: str, mensaje_caja: str, proveedor: str = "") -> None:
        self.motivo = motivo
        self.mensaje_caja = mensaje_caja
        super().__init__(
            provider=proveedor or "topups",
            message=mensaje_caja,
            missing_requirements=("Saldo suficiente en el proveedor",),
        )


def _ttl() -> int:
    return int(getattr(settings, "TOPUP_SALDO_CACHE_SECONDS", 30))


def _reserva() -> int:
    return int(getattr(settings, "TOPUP_RESERVA_SALDO_CENTS", 0))


def consultar(proveedor: "TopupProvider", *, usar_cache: bool = True) -> reglas.SaldoProveedor:
    """Saldo del proveedor, cacheado unos segundos por proveedor y modo.

    La clave incluye el modo para que un saldo de sandbox no se lea nunca
    como si fuera de produccion.
    """
    llave = f"{PREFIJO_CACHE}{proveedor.slug}:{proveedor.mode}"
    if usar_cache:
        guardado = cache.get(llave)
        if isinstance(guardado, reglas.SaldoProveedor):
            return guardado

    consultado = proveedor.saldo_disponible()
    # Tambien se cachea el "no se sabe": si el proveedor esta caido, no tiene
    # sentido volver a preguntarle en cada venta de la racha.
    cache.set(llave, consultado, timeout=_ttl())
    return consultado


def verificar_o_fallar(proveedor: "TopupProvider", monto: Money) -> reglas.Veredicto:
    """Levanta ``SaldoInsuficiente`` si no se puede vender. Devuelve el veredicto si si.

    Se devuelve el veredicto incluso cuando permite vender para que quien
    llame pueda registrarlo: un "se vendio sin poder verificar el saldo" es
    exactamente lo que hay que poder buscar despues en los registros.
    """
    productivo = proveedor.mode == ProviderMode.PRODUCTION

    estado = consultar(proveedor)
    veredicto = reglas.verificar(
        estado,
        monto,
        ambiente_productivo=productivo,
        reserva_cents=_reserva(),
    )

    if reglas.bloquea(veredicto, ambiente_productivo=productivo):
        log.error(
            "saldo_bloquea_la_venta",
            proveedor=proveedor.slug,
            modo=str(proveedor.mode),
            suficiencia=str(veredicto.suficiencia),
            monto_cents=monto.cents,
            motivo=veredicto.motivo,
        )
        raise SaldoInsuficiente(
            motivo=veredicto.motivo,
            mensaje_caja=veredicto.mensaje_caja or "Temporalmente no disponible",
            proveedor=proveedor.slug,
        )

    if not veredicto.permite_vender:
        # Pasa, pero queda escrito. En sandbox esto es lo normal cuando el
        # monedero esta en otra moneda que la venta.
        log.warning(
            "saldo_sin_verificar_se_permite",
            proveedor=proveedor.slug,
            modo=str(proveedor.mode),
            suficiencia=str(veredicto.suficiencia),
            motivo=veredicto.motivo,
        )

    return veredicto


def invalidar(proveedor: "TopupProvider") -> None:
    """Borra el saldo cacheado. Se llama tras una recarga, que lo movio."""
    cache.delete(f"{PREFIJO_CACHE}{proveedor.slug}:{proveedor.mode}")
