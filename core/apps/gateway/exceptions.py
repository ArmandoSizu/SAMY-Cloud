"""Manejo de errores de la API del Core Platform.

La implementacion vive en ``samy_common.api.errors`` para que los cuatro
servicios devuelvan errores con exactamente el mismo formato. Duplicarla por
servicio garantizaria que con el tiempo divergieran y que el frontend tuviera
que manejar cuatro formatos distintos.
"""

from __future__ import annotations

from samy_common.api.errors import samy_exception_handler  # noqa: F401

__all__ = ["samy_exception_handler"]
