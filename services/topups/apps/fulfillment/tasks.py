"""Tareas del microservicio de recargas.

``consume_payment_events`` es el disparador de toda ejecucion: escucha el
stream de eventos del servicio de Pagos y, al ver un ``order.paid`` que
corresponde a una recarga, la encola.

Detalle importante: el evento NO se toma como prueba de pago. Solo indica
"revisa esta orden". La comprobacion real la hace ``execute_topup()``
preguntandole al servicio de Pagos. Asi, ni un evento duplicado ni uno
retrasado ni uno manipulado pueden provocar una recarga sin cobro.
"""

from __future__ import annotations

import redis
import structlog
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.fulfillment.models import TopupFulfillment
from apps.fulfillment.services import execute_topup
from apps.providers.base import TopupStatus
from apps.providers.registry import get_provider
from samy_common.events.bus import Event, RedisStreamBus
from samy_common.providers.exceptions import ProviderError
from samy_common.states import FulfillmentState

log = structlog.get_logger("fulfillment.tasks")

#: Cuantos mensajes se leen por pasada. Suficiente para no acumular retraso
#: sin bloquear el worker demasiado tiempo en una sola tarea.
BATCH_SIZE = 20


@shared_task(name="apps.fulfillment.tasks.consume_payment_events")
def consume_payment_events() -> dict[str, int]:
    """Lee eventos de Pagos y encola las recargas ya pagadas."""
    client = redis.Redis.from_url(settings.EVENT_STREAM_URL)
    bus = RedisStreamBus(client, settings.PAYMENTS_STREAM_NAME)
    bus.ensure_group(settings.EVENT_CONSUMER_GROUP)

    processed = 0
    queued = 0

    messages = client.xreadgroup(
        groupname=settings.EVENT_CONSUMER_GROUP,
        consumername=f"{settings.SERVICE_NAME}-worker",
        streams={settings.PAYMENTS_STREAM_NAME: ">"},
        count=BATCH_SIZE,
        block=1000,
    )

    for _stream, entries in messages or []:
        for message_id, raw in entries:
            try:
                event = Event.from_wire(raw)
            except Exception as exc:  # noqa: BLE001
                # Un mensaje ilegible se confirma para que no bloquee la cola,
                # pero se registra como error para investigarlo.
                log.error("event_unparseable", message_id=str(message_id), error=str(exc))
                client.xack(settings.PAYMENTS_STREAM_NAME, settings.EVENT_CONSUMER_GROUP, message_id)
                continue

            try:
                if event.event_type == "order.paid":
                    if _handle_order_paid(event):
                        queued += 1
                processed += 1
            except Exception as exc:  # noqa: BLE001
                # Sin XACK: el mensaje queda pendiente y se reentrega. Perder
                # un order.paid significa un cliente que pago y no recibio
                # nada, asi que nunca se descarta por una excepcion.
                log.error(
                    "payment_event_handling_failed",
                    event_type=event.event_type,
                    aggregate_id=event.aggregate_id,
                    error=str(exc),
                    exc_info=True,
                )
                continue

            client.xack(
                settings.PAYMENTS_STREAM_NAME, settings.EVENT_CONSUMER_GROUP, message_id
            )

    if processed:
        log.info("payment_events_consumed", processed=processed, queued=queued)
    return {"processed": processed, "queued": queued}


def _handle_order_paid(event: Event) -> bool:
    """Encola la recarga asociada a una orden pagada."""
    payload = event.payload
    if payload.get("service_kind") != "TOPUP":
        return False

    fulfillment_id = payload.get("fulfillment_id")
    if not fulfillment_id:
        log.error("order_paid_without_fulfillment_id", order_id=payload.get("order_id"))
        return False

    fulfillment = TopupFulfillment.objects.filter(pk=fulfillment_id).first()
    if fulfillment is None:
        log.error("fulfillment_not_found", fulfillment_id=fulfillment_id)
        return False

    # Idempotencia del consumidor: si ya se ejecuto, un evento repetido no
    # vuelve a recargar.
    if fulfillment.state_enum not in {
        FulfillmentState.PENDING_PAYMENT,
        FulfillmentState.QUEUED,
    }:
        log.info(
            "fulfillment_already_processed",
            fulfillment_id=fulfillment_id,
            state=fulfillment.state,
        )
        return False

    if not fulfillment.order_id:
        fulfillment.order_id = payload.get("order_id")
        fulfillment.save(update_fields=["order_id", "updated_at"])

    execute_topup_task.delay(str(fulfillment.id))
    return True


@shared_task(
    name="apps.fulfillment.tasks.execute_topup_task",
    bind=True,
    max_retries=3,
    # Reintentos solo ante fallos transitorios; la propia funcion distingue.
    autoretry_for=(),
)
def execute_topup_task(self, fulfillment_id: str) -> dict[str, str]:
    """Ejecuta una recarga concreta."""
    fulfillment = TopupFulfillment.objects.filter(pk=fulfillment_id).first()
    if fulfillment is None:
        log.error("fulfillment_not_found_on_execute", fulfillment_id=fulfillment_id)
        return {"status": "not_found"}

    try:
        result = execute_topup(fulfillment=fulfillment)
    except ProviderError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            # Backoff exponencial: 10 s, 40 s, 90 s.
            countdown = 10 * (2**self.request.retries) ** 2 // 2
            log.warning(
                "topup_retry_scheduled",
                fulfillment_id=fulfillment_id,
                attempt=self.request.retries + 1,
                countdown=countdown,
            )
            raise self.retry(exc=exc, countdown=countdown)
        log.error(
            "topup_execution_failed",
            fulfillment_id=fulfillment_id,
            code=exc.code,
            error=exc.message,
        )
        return {"status": "failed", "reason": exc.message}

    return {"status": result.state}


@shared_task(name="apps.fulfillment.tasks.reconcile_pending_topups")
def reconcile_pending_topups(batch_size: int = 50) -> dict[str, int]:
    """Resuelve recargas cuyo resultado quedo desconocido.

    Consulta al proveedor el estado REAL. Nunca reintenta a ciegas: si la
    recarga ya se aplico, un reintento la duplicaria y ese dinero no se
    recupera.
    """
    resolved_ok = 0
    resolved_failed = 0
    still_unknown = 0

    # Recargas en revision, y enviadas hace mas de 2 minutos sin desenlace.
    cutoff = timezone.now() - timezone.timedelta(minutes=2)
    pending = TopupFulfillment.objects.filter(
        state__in=[FulfillmentState.UNDER_REVIEW, FulfillmentState.SENT],
        updated_at__lte=cutoff,
    ).order_by("created_at")[:batch_size]

    for fulfillment in pending:
        reference = fulfillment.provider_reference or fulfillment.idempotency_key
        if not reference:
            still_unknown += 1
            continue

        try:
            provider = get_provider(fulfillment.provider_slug or None)
            result = provider.get_topup_status(reference)
        except ProviderError as exc:
            still_unknown += 1
            log.warning(
                "topup_reconciliation_query_failed",
                fulfillment_id=str(fulfillment.id),
                error=exc.message,
            )
            continue

        if result.status == TopupStatus.SUCCEEDED:
            fulfillment.provider_reference = result.provider_reference[:128]
            fulfillment.operator_reference = result.operator_reference[:128]
            fulfillment.save(
                update_fields=["provider_reference", "operator_reference", "updated_at"]
            )
            fulfillment.transition(
                FulfillmentState.SUCCEEDED, reason="Conciliacion: el operador confirmo."
            )
            _notify(fulfillment, succeeded=True)
            resolved_ok += 1
        elif result.status == TopupStatus.FAILED:
            fulfillment.failure_reason = (
                result.failure_reason or "Conciliacion: el operador rechazo."
            )[:255]
            fulfillment.save(update_fields=["failure_reason", "updated_at"])
            fulfillment.transition(
                FulfillmentState.FAILED, reason=fulfillment.failure_reason
            )
            _notify(fulfillment, succeeded=False)
            resolved_failed += 1
        else:
            still_unknown += 1

    if resolved_ok or resolved_failed or still_unknown:
        log.info(
            "topup_reconciliation_sweep",
            resolved_ok=resolved_ok,
            resolved_failed=resolved_failed,
            still_unknown=still_unknown,
        )
    return {
        "resolved_ok": resolved_ok,
        "resolved_failed": resolved_failed,
        "still_unknown": still_unknown,
    }


def _notify(fulfillment: TopupFulfillment, *, succeeded: bool) -> None:
    from apps.outbox.models import OutboxEvent

    OutboxEvent.objects.create(
        event_type="fulfillment.result",
        aggregate_type="TopupFulfillment",
        aggregate_id=fulfillment.id,
        correlation_id=fulfillment.correlation_id,
        payload={
            "fulfillment_id": str(fulfillment.id),
            "order_id": str(fulfillment.order_id) if fulfillment.order_id else None,
            "succeeded": succeeded,
            "provider_reference": fulfillment.provider_reference,
            "operator_reference": fulfillment.operator_reference,
            "failure_reason": fulfillment.failure_reason,
            "state": fulfillment.state,
        },
    )
