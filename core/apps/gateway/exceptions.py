"""Manejo centralizado de errores de la API.

Objetivos:

1. **Formato unico de error** para toda la API, de modo que el frontend tenga
   un solo camino de manejo.
2. **No filtrar detalles internos** al cliente: trazas, SQL y mensajes de
   proveedores externos se quedan en los logs, no en la respuesta.
3. **Traducir excepciones de proveedor** a codigos estables que la UI pueda
   mostrar en espanol y con una accion sugerida.

Formato de respuesta:

    {
      "error": {
        "code": "provider_not_configured",
        "message": "Mensaje apto para mostrar al usuario.",
        "retryable": false,
        "correlation_id": "abc123",
        "details": {...}
      }
    }
"""

from __future__ import annotations

from typing import Any

import structlog
from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from samy_common.providers.exceptions import (
    ProviderConfigurationError,
    ProviderError,
    ProviderIndeterminateError,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.states import IllegalTransition

log = structlog.get_logger("api")

#: Mensajes en espanol, aptos para mostrar a un cajero. El detalle tecnico se
#: queda en los logs; el cajero necesita saber que hacer, no por que fallo.
_USER_MESSAGES: dict[type[Exception], str] = {
    ProviderNotConfigured: (
        "Esta integracion todavia no esta habilitada. "
        "Avisa al administrador de la tienda."
    ),
    ProviderTransientError: (
        "El proveedor no respondio. Espera unos segundos e intenta de nuevo. "
        "No se realizo ningun cargo."
    ),
    ProviderPermanentError: (
        "El proveedor rechazo la operacion. Verifica los datos capturados."
    ),
    ProviderIndeterminateError: (
        "No pudimos confirmar el resultado con el proveedor. "
        "La operacion quedo en revision: NO la repitas, consulta el historial."
    ),
    IllegalTransition: (
        "Esta operacion ya no admite esa accion en su estado actual."
    ),
}


def _error_body(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: Any = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
    }
    if correlation_id:
        body["error"]["correlation_id"] = correlation_id
    if details is not None:
        body["error"]["details"] = details
    return body


def samy_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Manejador de excepciones de DRF para todos los servicios."""
    request = context.get("request")
    correlation_id = getattr(request, "correlation_id", "") if request else ""

    # --- Errores de proveedor externo ---------------------------------
    if isinstance(exc, ProviderError):
        user_message = next(
            (msg for cls, msg in _USER_MESSAGES.items() if isinstance(exc, cls)),
            "No se pudo completar la operacion con el proveedor.",
        )

        if isinstance(exc, ProviderIndeterminateError):
            http_status = status.HTTP_202_ACCEPTED
            log_level = "error"
        elif isinstance(exc, ProviderConfigurationError):
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
            log_level = "warning"
        elif isinstance(exc, ProviderTransientError):
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
            log_level = "warning"
        else:
            http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
            log_level = "info"

        getattr(log, log_level)(
            "provider_error",
            provider=exc.provider,
            code=exc.code,
            detail=exc.message,
            external_code=exc.external_code,
        )

        details: dict[str, Any] = {"provider": exc.provider}
        if isinstance(exc, ProviderNotConfigured):
            # Al administrador si le decimos exactamente que falta.
            details["missing_requirements"] = list(exc.missing_requirements)

        return Response(
            _error_body(
                exc.code,
                user_message,
                retryable=exc.retryable,
                details=details,
                correlation_id=correlation_id,
            ),
            status=http_status,
        )

    # --- Transicion de estado invalida ---------------------------------
    if isinstance(exc, IllegalTransition):
        log.warning(
            "illegal_transition",
            current=exc.current,
            target=exc.target,
            allowed=sorted(exc.allowed),
        )
        return Response(
            _error_body(
                "illegal_transition",
                _USER_MESSAGES[IllegalTransition],
                details={"current_state": exc.current, "attempted_state": exc.target},
                correlation_id=correlation_id,
            ),
            status=status.HTTP_409_CONFLICT,
        )

    # --- Errores de Django que DRF no traduce solo --------------------
    if isinstance(exc, DjangoValidationError):
        return Response(
            _error_body(
                "validation_error",
                "Revisa los datos capturados.",
                details=exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
                correlation_id=correlation_id,
            ),
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, PermissionDenied):
        log.warning("permission_denied", path=getattr(request, "path", ""))
        return Response(
            _error_body(
                "permission_denied",
                "Tu rol no tiene acceso a esta operacion.",
                correlation_id=correlation_id,
            ),
            status=status.HTTP_403_FORBIDDEN,
        )

    if isinstance(exc, Http404):
        return Response(
            _error_body(
                "not_found",
                "No encontramos ese registro.",
                correlation_id=correlation_id,
            ),
            status=status.HTTP_404_NOT_FOUND,
        )

    # --- Resto: se delega en DRF y se normaliza el formato ------------
    response = drf_exception_handler(exc, context)
    if response is None:
        # Excepcion no controlada: se registra completa y se devuelve un
        # mensaje generico. Nunca se expone la traza al cliente.
        log.error("unhandled_exception", error=str(exc), exc_info=True)
        return Response(
            _error_body(
                "internal_error",
                "Ocurrio un error inesperado. El equipo fue notificado.",
                correlation_id=correlation_id,
            ),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    detail = response.data
    code = getattr(exc, "default_code", "error")
    message = "Revisa los datos capturados."
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
        detail = None

    response.data = _error_body(
        str(code), message, details=detail, correlation_id=correlation_id
    )
    return response
