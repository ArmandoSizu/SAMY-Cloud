"""Middleware de correlacion y contexto de peticion.

Se instala en todos los servicios. Su trabajo:

1. Aceptar o generar ``X-Request-ID`` y ``X-Correlation-ID``.
2. Publicarlos en el contexto de logging para toda la peticion.
3. Devolverlos en la respuesta, para que el navegador y los servicios aguas
   abajo puedan encadenar la traza.
4. Registrar un evento de acceso con duracion.

Nota de seguridad: los identificadores entrantes se **sanean** (solo hex y
guiones, longitud acotada). Aceptar un header arbitrario y meterlo en los logs
permite inyeccion de lineas de log falsas.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Callable, Final

import structlog
from django.http import HttpRequest, HttpResponse

from samy_common.observability.logging import correlation_id_var, request_id_var

__all__ = ["CorrelationMiddleware", "HEADER_REQUEST_ID", "HEADER_CORRELATION_ID"]

HEADER_REQUEST_ID: Final[str] = "X-Request-ID"
HEADER_CORRELATION_ID: Final[str] = "X-Correlation-ID"

_SAFE_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9\-_]{1,64}$")

#: Rutas que no generan log de acceso, para no ahogar los logs.
_QUIET_PATHS: Final[tuple[str, ...]] = ("/health", "/health/", "/static/", "/favicon.ico")


def _sanitize(value: str | None) -> str | None:
    if value and _SAFE_ID.match(value):
        return value
    return None


class CorrelationMiddleware:
    """Asigna request_id y correlation_id y registra el acceso."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = _sanitize(request.headers.get(HEADER_REQUEST_ID)) or uuid.uuid4().hex
        correlation_id = (
            _sanitize(request.headers.get(HEADER_CORRELATION_ID)) or request_id
        )

        request_id_var.set(request_id)
        correlation_id_var.set(correlation_id)
        request.request_id = request_id  # type: ignore[attr-defined]
        request.correlation_id = correlation_id  # type: ignore[attr-defined]

        structlog.contextvars.bind_contextvars(
            request_id=request_id, correlation_id=correlation_id
        )

        started = time.perf_counter()
        try:
            response = self.get_response(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "correlation_id")

        duration_ms = int((time.perf_counter() - started) * 1000)
        response[HEADER_REQUEST_ID] = request_id
        response[HEADER_CORRELATION_ID] = correlation_id

        if not request.path.startswith(_QUIET_PATHS):
            log = structlog.get_logger("access")
            log.info(
                "http_request",
                method=request.method,
                path=request.path,
                status=response.status_code,
                duration_ms=duration_ms,
                request_id=request_id,
                correlation_id=correlation_id,
                user_id=(
                    str(request.user.pk)
                    if getattr(request, "user", None)
                    and request.user.is_authenticated
                    else None
                ),
            )
        return response
