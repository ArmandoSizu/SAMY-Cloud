"""Manejo de errores de la API del microservicio de pagos.

Reutiliza el manejador del nucleo compartido para que los cuatro servicios
devuelvan errores con el mismo formato.
"""

from __future__ import annotations

from samy_common.api.errors import samy_exception_handler  # noqa: F401

__all__ = ["samy_exception_handler"]
