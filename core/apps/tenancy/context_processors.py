"""Expone la tienda activa y las tiendas disponibles a las plantillas."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest


def current_store(request: HttpRequest) -> dict[str, Any]:
    membership = getattr(request, "membership", None)
    user = getattr(request, "user", None)

    available = []
    if user is not None and user.is_authenticated:
        available = list(user.active_memberships())

    return {
        "current_store": getattr(request, "store", None),
        "current_membership": membership,
        "current_role": membership.role if membership else None,
        "available_memberships": available,
        "can_switch_store": len(available) > 1,
    }
