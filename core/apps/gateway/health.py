"""Endpoints de salud.

Se distinguen tres, porque responden preguntas distintas:

* ``/health/`` (**liveness**) - "¿el proceso esta vivo?". No toca la base de
  datos ni la red. Si falla, el orquestador reinicia el contenedor. Debe ser
  barato: se consulta cada pocos segundos.

* ``/health/ready/`` (**readiness**) - "¿puede atender trafico?". Comprueba
  base de datos y cache. Si falla, el balanceador saca la instancia de
  rotacion pero NO la reinicia. Confundir ambos provoca reinicios en cadena
  cuando lo que esta caido es la base de datos.

* ``/health/services/`` - estado de los microservicios y de las integraciones
  con proveedores. Es informativo, para el panel de plataforma. Requiere
  autenticacion porque revela topologia interna.
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

from apps.gateway.clients import all_clients
from apps.tenancy.permissions import user_has_perm
from samy_common.providers.exceptions import ProviderError

log = structlog.get_logger("health")


@require_GET
@never_cache
def liveness(request: HttpRequest) -> JsonResponse:
    """El proceso responde. Nada mas."""
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
    """Dependencias propias listas: base de datos y cache."""
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
        probe_key = "samy:health:probe"
        cache.set(probe_key, "1", timeout=10)
        if cache.get(probe_key) != "1":
            raise RuntimeError("La cache no devolvio el valor escrito.")
        checks["cache"] = {
            "status": "ok",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        healthy = False
        checks["cache"] = {"status": "error", "detail": str(exc)[:200]}

    if not healthy:
        log.error("readiness_failed", checks=checks)

    return JsonResponse(
        {
            "status": "ok" if healthy else "degraded",
            "service": settings.SERVICE_NAME,
            "checks": checks,
        },
        status=200 if healthy else 503,
    )


@require_GET
@never_cache
def services_health(request: HttpRequest) -> JsonResponse:
    """Estado de los microservicios y sus integraciones.

    Requiere permiso de plataforma: la lista de servicios y el estado de las
    integraciones es informacion de topologia interna.
    """
    if not user_has_perm(request, "platform.manage_providers"):
        return JsonResponse(
            {"error": {"code": "permission_denied", "message": "Acceso restringido."}},
            status=403,
        )

    results: dict[str, Any] = {}
    for name, client in all_clients().items():
        started = time.perf_counter()
        try:
            response = client.get("/health/ready/")
            results[name] = {
                "status": "ok" if response.ok else "degraded",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "detail": response.data,
            }
        except ProviderError as exc:
            results[name] = {
                "status": "unreachable",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "detail": exc.message,
            }

    overall = (
        "ok" if all(r["status"] == "ok" for r in results.values()) else "degraded"
    )
    return JsonResponse({"status": overall, "services": results})
