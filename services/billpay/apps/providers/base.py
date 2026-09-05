"""Contrato de los proveedores de pago de servicios.

CFE, CAPDAM y practicamente todos los organismos operadores de agua en Mexico
NO exponen API propia. La integracion pasa siempre por un agregador con
convenio. Este contrato modela esa realidad: un solo adaptador atiende muchos
billers, no uno por empresa.

Regla comun a todo el proyecto: sin credenciales, ``ensure_ready()`` levanta
y NO se cobra ni se paga nada.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from samy_common.money import Money
from samy_common.providers.base import BaseProvider

__all__ = [
    "BillerProvider",
    "BillerCatalogItem",
    "BillInquiry",
    "BillPaymentRequest",
    "BillPaymentResult",
    "BillPaymentStatus",
]


class BillPaymentStatus:
    SUCCEEDED = "SUCCEEDED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    #: Desconocido: se consulta, nunca se supone.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BillerCatalogItem:
    """Un servicio pagable, tal como lo publica el agregador."""

    provider_slug: str
    provider_biller_id: str
    name: str
    category: str
    supports_inquiry: bool = False
    supports_partial_payment: bool = False
    coverage_note: str = ""
    state_code: str = ""
    logo_url: str = ""
    #: Especificacion de la referencia SEGUN EL AGREGADOR. Nunca inventada.
    reference_label: str = "Referencia"
    reference_min_length: int = 1
    reference_max_length: int = 40
    reference_regex: str = ""
    reference_numeric_only: bool = True
    barcode_formats: tuple[str, ...] = ()
    min_amount: Money | None = None
    max_amount: Money | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BillInquiry:
    """Resultado de consultar un adeudo.

    ``found=False`` significa que el agregador respondio y NO encontro el
    recibo. Es un resultado valido y definitivo, distinto de un error de red:
    con found=False se le dice al cajero que revise la referencia, no que
    reintente.
    """

    found: bool
    reference: str
    biller_id: str
    amount_due: Money | None = None
    due_date: date | None = None
    customer_name: str = ""
    period: str = ""
    is_overdue: bool = False
    allows_partial: bool = False
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BillPaymentRequest:
    """Peticion de pago. Solo se construye con la orden ya PAGADA."""

    fulfillment_id: uuid.UUID
    order_id: uuid.UUID
    biller_id: str
    reference: str
    amount: Money
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class BillPaymentResult:
    status: str
    provider_reference: str
    provider_mode: str
    #: Folio que entrega el biller. Es lo que el cliente presenta si reclama.
    biller_reference: str = ""
    paid_amount: Money | None = None
    failure_reason: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == BillPaymentStatus.SUCCEEDED


class BillerProvider(BaseProvider[Any], abc.ABC):
    """Contrato de un agregador de pago de servicios."""

    @abc.abstractmethod
    def fetch_billers(self) -> list[BillerCatalogItem]:
        """Catalogo real de servicios pagables y sus formatos de referencia."""
        raise NotImplementedError

    @abc.abstractmethod
    def inquire(self, *, biller_id: str, reference: str) -> BillInquiry:
        """Consulta el adeudo. No cobra nada."""
        raise NotImplementedError

    @abc.abstractmethod
    def pay(self, request: BillPaymentRequest) -> BillPaymentResult:
        """Ejecuta el pago. Debe ser idempotente por ``idempotency_key``."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_payment_status(self, provider_reference: str) -> BillPaymentResult:
        """Consulta el estado real. Mecanismo de conciliacion."""
        raise NotImplementedError
