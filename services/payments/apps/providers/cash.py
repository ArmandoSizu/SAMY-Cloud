"""Cobro en efectivo en el mostrador.

Este adaptador **si opera de verdad y sin credenciales externas**, porque no
hay ningun tercero involucrado: el cliente entrega billetes al cajero.

Es importante entender por que esto NO viola la regla de "no simular pagos":

* En un cobro con tarjeta, la verdad la tiene el banco. Afirmar "pagado" sin
  su confirmacion seria inventarla.
* En un cobro en efectivo, **la verdad la tiene el cajero**, que es un usuario
  autenticado del sistema y esta fisicamente frente al cliente. Su
  confirmacion no es una simulacion: es el registro de un hecho real, con
  responsable identificado, monto entregado, cambio calculado y auditoria.

Es exactamente como opera cualquier punto de venta del pais.

Salvaguardas que lo hacen contable y no un boton de "marcar como pagado":

* Se exige registrar **cuanto entrego el cliente**, y el sistema calcula el
  cambio. No se puede confirmar sin recibir al menos el total.
* Queda asentado quien lo confirmo, cuando y desde que sesion.
* La confirmacion es idempotente: pulsar dos veces no cobra dos veces.
* Alimenta el arqueo de caja del turno, que el dueno puede cuadrar contra el
  dinero fisico. Un cajero que confirme cobros que no recibio produce un
  faltante visible en el arqueo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from apps.providers.base import (
    PaymentIntent,
    PaymentOutcome,
    PaymentProvider,
    PaymentResult,
    RefundResult,
)
from apps.providers.registry import payment_registry
from samy_common.money import Money
from samy_common.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderMode,
    ProviderStatus,
)
from samy_common.providers.exceptions import ProviderPermanentError

log = structlog.get_logger("provider.cash")


@dataclass(frozen=True, slots=True)
class CashConfig:
    enabled: bool = True


@payment_registry.register
class CashProvider(PaymentProvider):
    """Efectivo recibido en mostrador."""

    slug = "cash"
    display_name = "Efectivo en mostrador"
    capabilities = frozenset({ProviderCapability.CASH_PAYMENT, ProviderCapability.REFUND})
    required_settings = ()
    requires_commercial_contract = False
    documentation_url = None

    def __init__(self, config: CashConfig, mode: ProviderMode = ProviderMode.PRODUCTION):
        # El efectivo siempre es "produccion": el dinero fisico es real aunque
        # el resto del sistema este apuntando a sandboxes de terceros.
        super().__init__(config, ProviderMode.PRODUCTION)

    def check_health(self) -> ProviderHealth:
        """No hay dependencia externa: si esta habilitado, opera."""
        if not self.config.enabled:
            return ProviderHealth(
                status=ProviderStatus.DISABLED,
                detail="El cobro en efectivo esta deshabilitado para esta tienda.",
            )
        return ProviderHealth(
            status=ProviderStatus.READY,
            detail="Cobro en efectivo disponible. No requiere proveedor externo.",
            latency_ms=0,
        )

    def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Registra la intencion de cobro en efectivo.

        Devuelve ``PENDING``, nunca ``CONFIRMED``: el dinero todavia no esta
        en la caja. La confirmacion la da el cajero mediante
        ``confirm_cash_received()`` cuando recibe los billetes.
        """
        self.ensure_ready()

        log.info(
            "cash_payment_created",
            order_id=str(intent.order_id),
            folio=intent.folio,
            amount_cents=intent.amount.cents,
        )
        return PaymentResult(
            outcome=PaymentOutcome.PENDING,
            # La referencia es el propio folio: no hay identificador externo
            # porque no hay proveedor externo.
            provider_reference=f"CASH-{intent.folio}",
            provider_mode=str(self.mode),
            raw_response={
                "method": "cash",
                "awaiting": "confirmacion_del_cajero",
                "amount_cents": intent.amount.cents,
            },
        )

    def confirm_cash_received(
        self,
        *,
        intent_reference: str,
        amount_due: Money,
        amount_tendered: Money,
    ) -> tuple[PaymentResult, Money]:
        """Confirma la recepcion del efectivo. Devuelve el resultado y el cambio.

        Se rechaza si el cliente entrego menos del total: cobrar de menos y
        marcar la orden como pagada produciria un faltante de caja que nadie
        podria explicar despues.
        """
        self.ensure_ready()

        if amount_tendered.currency != amount_due.currency:
            raise ProviderPermanentError(
                provider=self.slug,
                message="La moneda entregada no coincide con la de la orden.",
            )

        if amount_tendered < amount_due:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    f"El efectivo recibido ({amount_tendered}) es menor al total "
                    f"a cobrar ({amount_due}). No se puede confirmar el pago."
                ),
            )

        change = amount_tendered - amount_due

        log.info(
            "cash_payment_confirmed",
            reference=intent_reference,
            due_cents=amount_due.cents,
            tendered_cents=amount_tendered.cents,
            change_cents=change.cents,
        )

        return (
            PaymentResult(
                outcome=PaymentOutcome.CONFIRMED,
                provider_reference=intent_reference,
                provider_mode=str(self.mode),
                raw_response={
                    "method": "cash",
                    "amount_due_cents": amount_due.cents,
                    "amount_tendered_cents": amount_tendered.cents,
                    "change_cents": change.cents,
                },
            ),
            change,
        )

    def get_payment_status(self, provider_reference: str) -> PaymentResult:
        """No hay a quien consultar: la verdad esta en nuestra base de datos.

        Devolver ``UNKNOWN`` es lo correcto: obliga a quien llama a resolver
        el estado con el registro local en vez de creerle a este adaptador.
        """
        return PaymentResult(
            outcome=PaymentOutcome.UNKNOWN,
            provider_reference=provider_reference,
            provider_mode=str(self.mode),
            raw_response={
                "note": (
                    "El efectivo no tiene proveedor externo. El estado real es "
                    "el que registra la orden en la base de datos local."
                )
            },
        )

    def refund(self, provider_reference: str, amount: Money) -> RefundResult:
        """Devolucion de efectivo desde la caja.

        Igual que el cobro: es un hecho fisico que el cajero registra, con
        responsable y auditoria, y que impacta el arqueo del turno.
        """
        self.ensure_ready()
        log.info(
            "cash_refund_registered",
            reference=provider_reference,
            amount_cents=amount.cents,
        )
        return RefundResult(
            succeeded=True,
            provider_reference=f"REFUND-{provider_reference}",
            amount=amount,
            raw_response={"method": "cash", "note": "Devolucion de efectivo en caja."},
        )
