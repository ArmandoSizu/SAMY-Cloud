"""Servicio de escritura de auditoria.

Se centraliza aqui para que registrar un evento sea una sola linea desde
cualquier vista y para garantizar que siempre se capturan los mismos campos
de contexto (IP, agente, correlacion, tienda, rol).

Regla operativa: **auditar nunca debe tumbar la operacion**. Si la escritura
del registro falla, se registra el fallo en el log y la operacion de negocio
continua. Perder una linea de auditoria es malo; perder el cobro del cliente
porque la tabla de auditoria estaba llena es peor.
"""

from __future__ import annotations

from typing import Any

import structlog
from django.http import HttpRequest

from apps.audit.models import AuditAction, AuditEvent

log = structlog.get_logger("audit")

#: Claves que jamas se guardan en ``metadata``, aunque quien llame las pase.
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "password",
        "cvv",
        "cvc",
        "card_number",
        "pan",
        "secret",
        "api_key",
        "token",
        "authorization",
    }
)


def _client_ip(request: HttpRequest) -> str | None:
    """IP real del cliente detras del proxy inverso.

    Se toma el PRIMER valor de X-Forwarded-For, que es el cliente original.
    Solo es fiable porque nuestro proxy reescribe la cabecera; confiar en ella
    sin un proxy de confianza permitiria falsificar la IP registrada.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _sanitize(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    return {
        key: ("[REDACTED]" if key.lower() in _FORBIDDEN_METADATA_KEYS else value)
        for key, value in metadata.items()
    }


def record(
    request: HttpRequest | None,
    action: str,
    *,
    store=None,
    object_type: str = "",
    object_id: str = "",
    previous_state: str = "",
    new_state: str = "",
    metadata: dict[str, Any] | None = None,
    actor=None,
) -> AuditEvent | None:
    """Escribe un evento de auditoria. Devuelve ``None`` si fallo."""
    try:
        user = actor
        if user is None and request is not None:
            candidate = getattr(request, "user", None)
            if candidate is not None and candidate.is_authenticated:
                user = candidate

        membership = getattr(request, "membership", None) if request else None
        resolved_store = store or (getattr(request, "store", None) if request else None)

        event = AuditEvent(
            action=action,
            actor=user,
            actor_email=getattr(user, "email", "") or "",
            actor_role=(membership.role if membership else ""),
            store=resolved_store,
            ip_address=_client_ip(request) if request else None,
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:255] if request else ""),
            session_key=(
                request.session.session_key or ""
                if request and hasattr(request, "session")
                else ""
            ),
            object_type=object_type,
            object_id=str(object_id),
            previous_state=previous_state,
            new_state=new_state,
            metadata=_sanitize(metadata),
            correlation_id=getattr(request, "correlation_id", "") if request else "",
        )
        event.save()
        return event
    except Exception as exc:  # noqa: BLE001
        # Nunca propagar: la auditoria no debe romper la operacion de negocio.
        log.error("audit_write_failed", action=action, error=str(exc), exc_info=True)
        return None


def record_login(request: HttpRequest, success: bool, email: str = "") -> None:
    record(
        request,
        AuditAction.LOGIN_SUCCESS if success else AuditAction.LOGIN_FAILED,
        metadata={"email": email} if email else None,
    )


def record_permission_denied(request: HttpRequest, permission: str) -> None:
    record(
        request,
        AuditAction.PERMISSION_DENIED,
        metadata={"permission": permission, "path": request.path},
    )
