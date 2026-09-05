"""Drenado del outbox hacia el bus de eventos.

Este es el proceso que convierte "el evento quedo guardado en la misma
transaccion" en "el evento efectivamente llego al otro microservicio".

Garantia de entrega: **al menos una vez**. Un evento puede publicarse dos
veces (si el proceso muere entre publicar y marcar como publicado), por lo
que todo consumidor debe ser idempotente. Es un compromiso deliberado:
"exactamente una vez" requiere coordinacion distribuida que no se justifica
aqui, y "como maximo una vez" perderia recargas pagadas.
"""

from __future__ import annotations

import redis
import structlog
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.outbox.models import OutboxEvent, OutboxStatus
from samy_common.events.bus import Event, RedisStreamBus

log = structlog.get_logger("outbox")


@shared_task(name="apps.outbox.tasks.drain_outbox")
def drain_outbox(batch_size: int = 50) -> dict[str, int]:
    """Publica los eventos pendientes cuyo momento de reintento ya llego."""
    client = redis.Redis.from_url(settings.EVENT_STREAM_URL)
    bus = RedisStreamBus(client, settings.EVENT_STREAM_NAME)

    published = 0
    failed = 0

    pending_ids = list(
        OutboxEvent.objects.filter(
            status=OutboxStatus.PENDING, next_attempt_at__lte=timezone.now()
        )
        .order_by("created_at")
        .values_list("id", flat=True)[:batch_size]
    )

    for event_id in pending_ids:
        # Se bloquea una fila a la vez: si hay varios workers drenando, cada
        # evento lo toma exactamente uno. skip_locked evita que se esperen
        # entre si.
        with transaction.atomic():
            event = (
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(id=event_id, status=OutboxStatus.PENDING)
                .first()
            )
            if event is None:
                continue

            try:
                bus.publish(
                    Event(
                        event_id=str(event.id),
                        event_type=event.event_type,
                        aggregate_type=event.aggregate_type,
                        aggregate_id=str(event.aggregate_id),
                        payload=event.payload,
                        correlation_id=event.correlation_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                backoff = min(
                    settings.OUTBOX_BASE_BACKOFF_SECONDS * (2**event.attempts), 600
                )
                event.mark_failed(str(exc), backoff_seconds=backoff)

                if event.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
                    event.status = OutboxStatus.FAILED
                    event.save(update_fields=["status"])
                    # Un evento que no se pudo publicar tras N intentos es un
                    # incidente: puede significar una recarga pagada que nunca
                    # se ejecuto. Se registra como error para que alerte.
                    log.error(
                        "outbox_event_permanently_failed",
                        event_id=str(event.id),
                        event_type=event.event_type,
                        aggregate_id=str(event.aggregate_id),
                        attempts=event.attempts,
                        error=str(exc),
                    )
            else:
                event.mark_published()
                published += 1

    if published or failed:
        log.info("outbox_drained", published=published, failed=failed)

    return {"published": published, "failed": failed}
