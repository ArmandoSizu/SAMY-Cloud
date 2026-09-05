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
        # Permisos que la navegacion necesita consultar. Se resuelven aqui,
        # con la misma funcion que protege las vistas, para que el menu no
        # pueda desincronizarse de lo que realmente esta permitido. Ocultar
        # un enlace nunca sustituye al permiso: la vista lo vuelve a exigir.
        "can_manage_employees": _has_perm(request, "store.manage_employees"),
    }


def _has_perm(request: HttpRequest, permission: str) -> bool:
    # Importacion diferida: permissions importa modelos, y este modulo lo
    # carga Django al construir las plantillas.
    from apps.tenancy.permissions import user_has_perm

    return user_has_perm(request, permission)
