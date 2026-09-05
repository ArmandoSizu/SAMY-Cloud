"""Logs estructurados en JSON con correlacion de peticiones.

En un sistema distribuido, un log de texto plano por servicio es inservible
para investigar por que una recarga concreta fallo. SAMY Cloud emite JSON con:

* ``request_id``  - identifica una peticion HTTP dentro de un servicio.
* ``correlation_id`` - identifica una **operacion de negocio completa** y viaja
  entre microservicios en la cabecera ``X-Correlation-ID``. Es lo que permite
  reconstruir "cajero pulso Recargas -> orden -> pago -> recarga -> comprobante"
  como una sola traza.
* ``service``, ``store_id``, ``order_id``, ``provider``.

Todo pasa por un procesador de saneado que elimina PAN y CVV aunque alguien
los incluya por error.
"""

from __future__ import annotations

import contextvars
import logging
import sys
from typing import Any

import structlog

from samy_common.security.masking import scrub_text

__all__ = [
    "configure_logging",
    "get_logger",
    "request_id_var",
    "correlation_id_var",
    "bind_context",
]

#: Contexto por peticion. ``contextvars`` funciona correctamente con async y
#: con hilos, a diferencia de una variable global o de ``threading.local``.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def _add_correlation(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    """Inyecta los identificadores de correlacion en cada evento."""
    if rid := request_id_var.get():
        event.setdefault("request_id", rid)
    if cid := correlation_id_var.get():
        event.setdefault("correlation_id", cid)
    return event


def _scrub_sensitive(
    _logger: Any, _name: str, event: dict[str, Any]
) -> dict[str, Any]:
    """Ultima linea de defensa contra fuga de datos de tarjeta en logs."""
    for key, value in list(event.items()):
        if isinstance(value, str):
            event[key] = scrub_text(value)
    return event


def _drop_forbidden_keys(
    _logger: Any, _name: str, event: dict[str, Any]
) -> dict[str, Any]:
    """Elimina claves que nunca deben registrarse, sin importar quien las puso."""
    forbidden = {
        "password",
        "cvv",
        "cvc",
        "card_number",
        "pan",
        "secret",
        "api_key",
        "authorization",
        "token",
        "private_key",
    }
    for key in list(event):
        if key.lower() in forbidden:
            event[key] = "[REDACTED]"
    return event


def configure_logging(
    *, service_name: str, level: str = "INFO", json_output: bool = True
) -> None:
    """Configura structlog + logging estandar para un servicio.

    ``json_output=False`` da salida coloreada legible, util solo en desarrollo
    local. En cualquier entorno desplegado se usa JSON para que el agregador
    de logs pueda indexar los campos.
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_correlation,
        _drop_forbidden_keys,
        _scrub_sensitive,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    structlog.contextvars.bind_contextvars(service=service_name)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    """Agrega campos al contexto de log de la peticion actual."""
    structlog.contextvars.bind_contextvars(**kwargs)
