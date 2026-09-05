"""Mantenimiento de la tabla de idempotencia."""

from __future__ import annotations

import structlog
from celery import shared_task
from django.utils import timezone

from apps.api.models import IdempotencyRecord

log = structlog.get_logger("idempotency")


@shared_task(name="apps.api.tasks.purge_expired_idempotency")
def purge_expired_idempotency() -> dict[str, int]:
    """Elimina registros vencidos.

    Sin esta purga la tabla crece indefinidamente y el indice UNIQUE, que es
    lo que da la garantia de idempotencia, se degrada.
    """
    deleted, _ = IdempotencyRecord.objects.filter(
        expires_at__lte=timezone.now()
    ).delete()
    if deleted:
        log.info("idempotency_records_purged", deleted=deleted)
    return {"deleted": deleted}
