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

# Celery autodescubre unicamente ``tasks.py`` de cada app. El consumidor de
# resultados de entrega vive en ``consumers.py`` porque no es una tarea de
# mantenimiento sino la otra mitad del ciclo de la orden; se importa aqui para
# que quede registrado. Sin esta linea la tarea no existe para el worker y las
# ordenes se quedan en PAID.
from apps.orders.consumers import consume_fulfillment_events  # noqa: F401
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


#: Cuanto se espera al webhook antes de ir a preguntar por nuestra cuenta.
#: Un webhook normal llega en segundos; dos minutos es holgado sin dejar al
#: cajero esperando un desenlace que ya existe del lado del proveedor.
ESPERA_ANTES_DE_PREGUNTAR = timezone.timedelta(minutes=2)


@shared_task(name="apps.orders.tasks.reconcile_pending_payments")
def reconcile_pending_payments(batch_size: int = 50) -> dict[str, int]:
    """Pregunta al proveedor por los cobros que quedaron sin desenlace.

    Cubre el hueco entre las otras dos barridas. ``expire_stale_orders`` solo
    mira ordenes cuyo plazo YA vencio, y ``reconcile_indeterminate_orders``
    solo las que quedaron marcadas como indeterminadas. Entremedio hay un caso
    muy real: el cobro salio, Conekta lo acepto, y el webhook no llego -o no
    puede llegar, como en local, donde Conekta no alcanza localhost-. Esa
    orden se queda en PAYMENT_PENDING con su referencia del proveedor, sin que
    nadie pregunte, hasta que expire.

    Aqui se pregunta antes. La consulta es de SOLO LECTURA sobre una
    referencia que ya existe: no crea cobros, no reenvia tokens y no puede
    duplicar nada. ``confirm_payment`` ademas ignora confirmaciones repetidas,
    asi que ejecutar esta barrida de mas es inofensivo.

    Lo que NO hace: asumir un fallo. Si el proveedor no contesta o contesta
    algo no terminal, la orden se queda como esta y se vuelve a intentar. Dar
    por fallado un cobro que si ocurrio es justo el error que arruina a un
    negocio pequeno.
    """
    corte = timezone.now() - ESPERA_ANTES_DE_PREGUNTAR

    pendientes = (
        PaymentAttempt.objects.filter(
            order__state=OrderState.PAYMENT_PENDING,
            status__in=[
                PaymentAttemptStatus.INITIATED,
                PaymentAttemptStatus.AWAITING_CUSTOMER,
            ],
            created_at__lte=corte,
        )
        .exclude(provider_slug="cash")
        .exclude(provider_reference="")
        .select_related("order")
        .order_by("created_at")[:batch_size]
    )

    confirmados = 0
    rechazados = 0
    sin_desenlace = 0

    for attempt in pendientes:
        try:
            provider = get_provider(attempt.provider_slug)
            result = provider.get_payment_status(attempt.provider_reference)
        except Exception as exc:  # noqa: BLE001
            sin_desenlace += 1
            log.warning(
                "pendiente_sin_poder_consultar",
                attempt_id=str(attempt.id),
                provider=attempt.provider_slug,
                error=str(exc),
            )
            continue

        if result.outcome == PaymentOutcome.CONFIRMED:
            services.confirm_payment(
                order=attempt.order,
                attempt=attempt,
                provider_reference=result.provider_reference
                or attempt.provider_reference,
                source="conciliacion_pendientes",
            )
            confirmados += 1
            log.warning(
                "cobro_confirmado_sin_webhook",
                order_id=str(attempt.order_id),
                folio=attempt.order.folio,
                detalle="El proveedor lo daba por pagado y el webhook no llego.",
            )
        elif result.outcome == PaymentOutcome.DECLINED:
            attempt.mark_failed("Conciliacion: el proveedor lo reporta rechazado.")
            rechazados += 1
            log.info(
                "cobro_rechazado_por_conciliacion",
                order_id=str(attempt.order_id),
                folio=attempt.order.folio,
            )
        else:
            # Sigue en curso de verdad. No se toca.
            sin_desenlace += 1

    if confirmados or rechazados or sin_desenlace:
        log.info(
            "conciliacion_pendientes",
            confirmados=confirmados,
            rechazados=rechazados,
            sin_desenlace=sin_desenlace,
        )
    return {
        "confirmados": confirmados,
        "rechazados": rechazados,
        "sin_desenlace": sin_desenlace,
    }
