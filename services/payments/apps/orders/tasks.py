"""Tareas periodicas de ordenes: expiracion y conciliacion.

Estas dos tareas son las que impiden que una orden se quede colgada para
siempre. Sin ellas, un timeout de un proveedor dejaria dinero en un limbo
que nadie revisa.
"""

from __future__ import annotations

import structlog
from celery import shared_task
from django.utils import timezone

from apps.orders import services
from apps.orders.models import Order
from apps.payments.models import PaymentAttempt, PaymentAttemptStatus
from apps.providers.base import PaymentOutcome
from apps.providers.registry import get_provider
from samy_common.states import OrderState

log = structlog.get_logger("orders.tasks")


@shared_task(name="apps.orders.tasks.expire_stale_orders")
def expire_stale_orders(batch_size: int = 100) -> dict[str, int]:
    """Cierra intentos de cobro vencidos.

    Antes de expirar consulta al proveedor: si el cliente pago en el ultimo
    segundo y el webhook se retraso, expirar sin preguntar le cobraria sin
    entregarle nada.
    """
    expired = 0
    recovered = 0

    stale = Order.objects.filter(
        state=OrderState.PAYMENT_PENDING, expires_at__lte=timezone.now()
    ).order_by("expires_at")[:batch_size]

    for order in stale:
        try:
            before = order.state
            result = services.expire_order(order=order)
            if result.state == OrderState.PAID:
                recovered += 1
                log.warning(
                    "order_recovered_before_expiry",
                    order_id=str(order.id),
                    folio=order.folio,
                    previous_state=before,
                )
            else:
                expired += 1
        except Exception as exc:  # noqa: BLE001
            log.error(
                "order_expiry_failed",
                order_id=str(order.id),
                error=str(exc),
                exc_info=True,
            )

    if expired or recovered:
        log.info("orders_expiry_sweep", expired=expired, recovered=recovered)
    return {"expired": expired, "recovered": recovered}


@shared_task(name="apps.orders.tasks.reconcile_indeterminate_orders")
def reconcile_indeterminate_orders(batch_size: int = 50) -> dict[str, int]:
    """Resuelve operaciones cuyo resultado quedo desconocido.

    Consulta el estado REAL al proveedor. Nunca adivina: si el proveedor
    tampoco responde, la orden se queda en revision y se reintenta despues.
    Una orden en revision es un problema visible; una orden cerrada con un
    estado inventado es un problema invisible, que es mucho peor.
    """
    resolved_paid = 0
    resolved_failed = 0
    still_unknown = 0

    attempts = (
        PaymentAttempt.objects.filter(status=PaymentAttemptStatus.INDETERMINATE)
        .exclude(provider_slug="cash")
        .select_related("order")
        .order_by("created_at")[:batch_size]
    )

    for attempt in attempts:
        try:
            provider = get_provider(attempt.provider_slug)
            # La consulta se hace por la referencia del proveedor o, si no
            # llego, por la clave de idempotencia con la que se envio.
            reference = attempt.provider_reference or attempt.idempotency_key
            result = provider.get_payment_status(reference)
        except Exception as exc:  # noqa: BLE001
            still_unknown += 1
            log.warning(
                "reconciliation_query_failed",
                attempt_id=str(attempt.id),
                provider=attempt.provider_slug,
                error=str(exc),
            )
            continue

        if result.outcome == PaymentOutcome.CONFIRMED:
            services.confirm_payment(
                order=attempt.order,
                attempt=attempt,
                provider_reference=result.provider_reference,
                source="conciliacion_automatica",
            )
            resolved_paid += 1
            log.info(
                "reconciliation_resolved_paid",
                order_id=str(attempt.order_id),
                folio=attempt.order.folio,
            )
        elif result.outcome == PaymentOutcome.DECLINED:
            attempt.mark_failed("Conciliacion: el proveedor confirmo que no se cobro.")
            attempt.order.transition(
                OrderState.EXPIRED,
                reason="Conciliacion: el cobro nunca se completo.",
            )
            resolved_failed += 1
        else:
            still_unknown += 1

    if resolved_paid or resolved_failed or still_unknown:
        log.info(
            "reconciliation_sweep",
            resolved_paid=resolved_paid,
            resolved_failed=resolved_failed,
            still_unknown=still_unknown,
        )
    return {
        "resolved_paid": resolved_paid,
        "resolved_failed": resolved_failed,
        "still_unknown": still_unknown,
    }
