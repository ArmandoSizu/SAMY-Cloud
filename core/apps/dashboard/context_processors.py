"""Identidad de marca disponible en todas las plantillas."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def branding(request: HttpRequest) -> dict[str, Any]:
    return {
        "BRAND_NAME": "SAMY",
        "BRAND_SUFFIX": "Cloud",
        "BRAND_FULL": "SAMY Cloud",
        "SERVICE_VERSION": getattr(settings, "SERVICE_VERSION", "0.1.0"),
        "IS_DEBUG": settings.DEBUG,
    }
