"""Contrato de los proveedores de pago.

Toda pasarela de pago se integra implementando ``PaymentProvider``. El resto
del sistema no conoce a Conekta, Stripe ni a nadie: solo conoce este contrato.
Cambiar de pasarela es escribir un adaptador nuevo, no reescribir el servicio.

La regla que gobierna todos los adaptadores:

    Si no hay credenciales, ``ensure_ready()`` levanta ``ProviderNotConfigured``
    y la operacion se detiene ANTES de tocar la orden. Nunca se devuelve un
    ``PaymentResult`` exitoso sin una confirmacion real del proveedor.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from samy_common.money import Money
from samy_common.providers.base import BaseProvider, ProviderCapability

__all__ = [
    "PaymentProvider",
    "PaymentIntent",
    "PaymentResult",
    "RefundResult",
    "WebhookEvent",
    "PaymentOutcome",
]


class PaymentOutcome:
    """Resultados posibles de un intento de cobro."""

    #: El dinero esta confirmado. Es el UNICO valor que autoriza ejecutar
    #: el servicio subyacente.
    CONFIRMED = "CONFIRMED"
    #: El proveedor acepto el intento pero el pago aun no ocurre (el cliente
    #: debe escanear un QR, transferir, o el 3DS sigue en curso).
    PENDING = "PENDING"
    #: Rechazado de forma definitiva.
    DECLINED = "DECLINED"
    #: No sabemos el resultado. Requiere consulta de estado, nunca suposicion.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    """Peticion de cobro que se envia al proveedor."""

    order_id: uuid.UUID
    folio: str
    amount: Money
    description: str
    #: Clave de idempotencia. El proveedor debe usarla para no cobrar dos veces.
    idempotency_key: str
    store_id: uuid.UUID
    method: str
    #: Vencimiento del intento (QR, referencia).
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """Respuesta del proveedor a un intento de cobro."""

    outcome: str
    #: Identificador del cargo en el proveedor. Es la clave para conciliar.
    provider_reference: str
    #: Ambiente real con el que se opero. Viaja hasta el comprobante.
    provider_mode: str
    #: Datos para completar el cobro del lado del cliente (URL de checkout,
    #: cadena del QR, referencia bancaria).
    checkout_url: str = ""
    qr_payload: str = ""
    #: Respuesta cruda, saneada, para auditoria y soporte.
    raw_response: dict[str, Any] = field(default_factory=dict)
    declined_reason: str = ""

    @property
    def is_confirmed(self) -> bool:
        return self.outcome == PaymentOutcome.CONFIRMED


@dataclass(frozen=True, slots=True)
class RefundResult:
    succeeded: bool
    provider_reference: str
    amount: Money
    raw_response: dict[str, Any] = field(default_factory=dict)
    failure_reason: str = ""


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """Evento entrante del proveedor, ya verificado criptograficamente."""

    event_id: str
    event_type: str
    provider_reference: str
    outcome: str
    amount: Money | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(BaseProvider[Any], abc.ABC):
    """Contrato de una pasarela de pago."""

    @abc.abstractmethod
    def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Inicia el cobro.

        Debe ser **idempotente**: dos llamadas con el mismo
        ``intent.idempotency_key`` producen un solo cargo y devuelven el mismo
        ``provider_reference``.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_payment_status(self, provider_reference: str) -> PaymentResult:
        """Consulta el estado real de un cobro.

        Es el mecanismo de conciliacion: cuando un timeout deja una operacion
        en estado desconocido, esta consulta es lo que decide, en vez de
        suponer. Todo adaptador DEBE implementarla.
        """
        raise NotImplementedError

    def refund(self, provider_reference: str, amount: Money) -> RefundResult:
        """Reembolsa. Por defecto no soportado."""
        self.require(ProviderCapability.REFUND)
        raise NotImplementedError(
            f"{self.display_name} declara REFUND pero no lo implemento."
        )

    def verify_webhook(
        self, *, payload: bytes, headers: dict[str, str]
    ) -> WebhookEvent:
        """Verifica la firma de un webhook y lo traduce a ``WebhookEvent``.

        Un webhook sin verificar es una instruccion de un desconocido para
        marcar una orden como pagada. Todo adaptador que declare
        ``WEBHOOK_SIGNED`` debe implementar esto, y debe FALLAR si la firma
        no valida, nunca aceptar el evento "por si acaso".
        """
        self.require(ProviderCapability.WEBHOOK_SIGNED)
        raise NotImplementedError(
            f"{self.display_name} declara WEBHOOK_SIGNED pero no lo implemento."
        )
