"""Maquina de estados para operaciones que mueven dinero.

Principio rector de SAMY Cloud:

    PRIMERO SE CONFIRMA EL PAGO. DESPUES SE EJECUTA EL SERVICIO.

La maquina de estados es la que hace cumplir esa regla a nivel de codigo: no
existe ninguna transicion que lleve de un estado no pagado a ``PROCESSING``.
Intentar ejecutar un servicio sin haber pasado por ``PAID`` levanta
``IllegalTransition`` antes de tocar al proveedor.

Las transiciones se validan en la aplicacion Y se restringen en base de datos
mediante ``CheckConstraint`` sobre el conjunto de valores permitidos.
"""

from __future__ import annotations

import enum
from typing import Final, Mapping

__all__ = [
    "OrderState",
    "FulfillmentState",
    "IllegalTransition",
    "ORDER_TRANSITIONS",
    "FULFILLMENT_TRANSITIONS",
    "assert_transition",
    "can_transition",
]


class IllegalTransition(Exception):
    """Se intento una transicion de estado no permitida."""

    def __init__(self, current: str, target: str, allowed: frozenset[str]) -> None:
        self.current = current
        self.target = target
        self.allowed = allowed
        super().__init__(
            f"Transicion invalida {current} -> {target}. "
            f"Permitidas desde {current}: {sorted(allowed) or 'ninguna (estado final)'}."
        )


class OrderState(enum.StrEnum):
    """Ciclo de vida de una **orden** (el lado del dinero).

    Vive en el microservicio de Pagos, que es el unico dueno de este agregado.
    """

    #: Orden creada, aun sin intento de cobro.
    CREATED = "CREATED"
    #: Se genero un intento de pago (checkout, QR, referencia). Esperando al cliente.
    PAYMENT_PENDING = "PAYMENT_PENDING"
    #: Pago CONFIRMADO por el proveedor (webhook firmado o cobro en efectivo
    #: registrado por el cajero). Recien aqui se puede ejecutar el servicio.
    PAID = "PAID"
    #: El servicio subyacente se esta ejecutando en su microservicio.
    PROCESSING = "PROCESSING"
    #: Pago confirmado Y servicio entregado. Estado final feliz.
    SUCCESS = "SUCCESS"
    #: El servicio fallo de forma definitiva. Si hubo cobro, pasa a reembolso.
    FAILED = "FAILED"
    #: Cancelada antes de cobrar.
    CANCELLED = "CANCELLED"
    #: El intento de pago expiro sin confirmarse (por ejemplo, QR vencido).
    EXPIRED = "EXPIRED"
    #: Se cobro pero el servicio no se entrego: hay que devolver el dinero.
    REFUND_PENDING = "REFUND_PENDING"
    #: Dinero devuelto y confirmado por el proveedor.
    REFUNDED = "REFUNDED"
    #: Estado desconocido tras un timeout. Requiere conciliacion manual o
    #: automatica antes de poder avanzar. NUNCA se resuelve adivinando.
    UNDER_REVIEW = "UNDER_REVIEW"


class FulfillmentState(enum.StrEnum):
    """Ciclo de vida de la **entrega del servicio** (recarga o pago de recibo).

    Vive en los microservicios de Recargas y Pago de Servicios. Es un agregado
    separado del de la orden: el dinero y la entrega tienen ciclos distintos y
    pueden fallar de forma independiente.
    """

    #: Registrada, esperando confirmacion de pago de la orden.
    PENDING_PAYMENT = "PENDING_PAYMENT"
    #: Orden pagada. En cola para ejecutarse contra el proveedor.
    QUEUED = "QUEUED"
    #: Peticion enviada al proveedor, esperando respuesta.
    SENT = "SENT"
    #: Proveedor confirmo la entrega. Estado final feliz.
    SUCCEEDED = "SUCCEEDED"
    #: Proveedor rechazo de forma definitiva.
    FAILED = "FAILED"
    #: Timeout o respuesta ambigua. Requiere consulta de estado al proveedor.
    UNDER_REVIEW = "UNDER_REVIEW"
    #: Revertida tras reembolso.
    REVERSED = "REVERSED"


#: Grafo de transiciones permitidas de la orden.
#:
#: Notese que ``CREATED`` y ``PAYMENT_PENDING`` NO conectan con ``PROCESSING``.
#: Esa ausencia es la implementacion literal de la regla del dinero.
ORDER_TRANSITIONS: Final[Mapping[OrderState, frozenset[OrderState]]] = {
    OrderState.CREATED: frozenset(
        {OrderState.PAYMENT_PENDING, OrderState.PAID, OrderState.CANCELLED}
    ),
    OrderState.PAYMENT_PENDING: frozenset(
        {
            OrderState.PAID,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.UNDER_REVIEW,
        }
    ),
    OrderState.PAID: frozenset({OrderState.PROCESSING, OrderState.REFUND_PENDING}),
    OrderState.PROCESSING: frozenset(
        {OrderState.SUCCESS, OrderState.FAILED, OrderState.UNDER_REVIEW}
    ),
    OrderState.FAILED: frozenset({OrderState.REFUND_PENDING}),
    OrderState.REFUND_PENDING: frozenset(
        {OrderState.REFUNDED, OrderState.UNDER_REVIEW}
    ),
    OrderState.UNDER_REVIEW: frozenset(
        {
            OrderState.PAID,
            OrderState.SUCCESS,
            OrderState.FAILED,
            OrderState.REFUND_PENDING,
            OrderState.EXPIRED,
        }
    ),
    # Estados finales
    OrderState.SUCCESS: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.REFUNDED: frozenset(),
}

#: Grafo de transiciones permitidas de la entrega del servicio.
FULFILLMENT_TRANSITIONS: Final[
    Mapping[FulfillmentState, frozenset[FulfillmentState]]
] = {
    FulfillmentState.PENDING_PAYMENT: frozenset(
        {FulfillmentState.QUEUED, FulfillmentState.FAILED}
    ),
    FulfillmentState.QUEUED: frozenset(
        {FulfillmentState.SENT, FulfillmentState.FAILED}
    ),
    FulfillmentState.SENT: frozenset(
        {
            FulfillmentState.SUCCEEDED,
            FulfillmentState.FAILED,
            FulfillmentState.UNDER_REVIEW,
        }
    ),
    FulfillmentState.UNDER_REVIEW: frozenset(
        {FulfillmentState.SUCCEEDED, FulfillmentState.FAILED}
    ),
    FulfillmentState.SUCCEEDED: frozenset({FulfillmentState.REVERSED}),
    FulfillmentState.FAILED: frozenset(),
    FulfillmentState.REVERSED: frozenset(),
}

#: Estados de orden en los que el dinero ya esta en nuestro poder.
MONEY_HELD_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.PAID,
        OrderState.PROCESSING,
        OrderState.SUCCESS,
        OrderState.FAILED,
        OrderState.REFUND_PENDING,
        OrderState.UNDER_REVIEW,
    }
)

#: Estados finales: no admiten mas transiciones.
TERMINAL_ORDER_STATES: Final[frozenset[OrderState]] = frozenset(
    s for s, targets in ORDER_TRANSITIONS.items() if not targets
)


def can_transition(
    current: OrderState | FulfillmentState,
    target: OrderState | FulfillmentState,
) -> bool:
    """Devuelve True si la transicion esta permitida."""
    table: Mapping = (
        ORDER_TRANSITIONS if isinstance(current, OrderState) else FULFILLMENT_TRANSITIONS
    )
    return target in table.get(current, frozenset())


def assert_transition(
    current: OrderState | FulfillmentState,
    target: OrderState | FulfillmentState,
) -> None:
    """Levanta ``IllegalTransition`` si la transicion no esta permitida."""
    table: Mapping = (
        ORDER_TRANSITIONS if isinstance(current, OrderState) else FULFILLMENT_TRANSITIONS
    )
    allowed = table.get(current, frozenset())
    if target not in allowed:
        raise IllegalTransition(
            str(current), str(target), frozenset(str(a) for a in allowed)
        )
