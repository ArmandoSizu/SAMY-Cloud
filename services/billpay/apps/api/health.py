"""Endpoints de salud del microservicio de pagos.

Exentos de firma S2S: el orquestador los consulta antes de que exista
cualquier credencial, y no revelan informacion de negocio.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

log = structlog.get_logger("health")


@require_GET
@never_cache
def liveness(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "status": "ok",
            "service": settings.SERVICE_NAME,
            "version": settings.SERVICE_VERSION,
        }
    )


@require_GET
@never_cache
def readiness(request: HttpRequest) -> JsonResponse:
    checks: dict[str, Any] = {}
    healthy = True

    started = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = {
            "status": "ok",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        healthy = False
        checks["database"] = {"status": "error", "detail": str(exc)[:200]}

    started = time.perf_counter()
    try:
        cache.set("samy:billpay:health", "1", timeout=10)
        if cache.get("samy:billpay:health") != "1":
            raise RuntimeError("La cache no devolvio el valor escrito.")
        checks["cache"] = {
            "status": "ok",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        healthy = False
        checks["cache"] = {"status": "error", "detail": str(exc)[:200]}

    # Backlog del outbox: si crece, los eventos no se estan publicando y las
    # recargas pagadas no llegan a ejecutarse. Es una senal temprana de que
    # algo va mal, mucho antes de que un cliente se queje.
    try:
        from apps.outbox.models import OutboxEvent, OutboxStatus

        pending = OutboxEvent.objects.filter(status=OutboxStatus.PENDING).count()
        checks["outbox_pending"] = {
            "status": "ok" if pending < 100 else "degraded",
            "count": pending,
        }
        if pending >= 100:
            healthy = False
            log.error("outbox_backlog_high", pending=pending)
    except Exception as exc:  # noqa: BLE001
        checks["outbox_pending"] = {"status": "unknown", "detail": str(exc)[:200]}

    return JsonResponse(
        {"status": "ok" if healthy else "degraded", "service": settings.SERVICE_NAME,
         "checks": checks},
        status=200 if healthy else 503,
    )
