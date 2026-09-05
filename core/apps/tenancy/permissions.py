"""Autorizacion basada en roles (RBAC) para SAMY Cloud.

Principio: **denegar por defecto**. Una vista sin decorador de permiso no es
accesible; no existe un camino en el que "se me olvido poner el permiso" deje
algo abierto, porque las vistas heredan de mixins que exigen declararlo.

Toda decision de autorizacion responde a dos preguntas, en este orden:

1. ¿El usuario pertenece a ESTA tienda? (aislamiento multi-tenant)
2. ¿Su rol en esa tienda incluye ESTE permiso? (RBAC)

Saltarse la primera es la fuga de datos clasica de un SaaS multi-inquilino:
un cajero de la tienda A que cambia un UUID en la URL y ve la venta de la B.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from rest_framework import permissions as drf_permissions

from apps.tenancy.models import Membership, Role


def get_current_membership(request: HttpRequest) -> Membership | None:
    """Membresia activa de la peticion, puesta por ``CurrentStoreMiddleware``."""
    return getattr(request, "membership", None)


def user_has_perm(request: HttpRequest, permission: str) -> bool:
    """Comprueba un permiso contra la tienda ACTIVA de la peticion."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return False
    # El administrador de plataforma tiene acceso transversal por diseno; es
    # el unico rol que lo tiene y sus acciones quedan siempre auditadas.
    if user.is_platform_admin:
        return True
    membership = get_current_membership(request)
    return bool(membership and membership.has_perm(permission))


def require_perm(permission: str) -> Callable:
    """Decorador de vistas basadas en funcion."""

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            if not user_has_perm(request, permission):
                raise PermissionDenied(
                    f"Tu rol no tiene el permiso '{permission}' en esta tienda."
                )
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


class PermissionRequiredMixin(AccessMixin):
    """Mixin para vistas basadas en clase.

    Falla ruidosamente si la subclase olvida declarar ``required_permission``:
    es preferible un error 500 en desarrollo a una vista abierta en produccion.
    """

    required_permission: str | None = None

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if self.required_permission is None:
            raise ImproperlyConfiguredPermission(
                f"{type(self).__name__} no declara 'required_permission'. "
                "Toda vista debe declarar explicitamente el permiso que exige."
            )
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not user_has_perm(request, self.required_permission):
            raise PermissionDenied(
                f"Tu rol no tiene el permiso '{self.required_permission}'."
            )
        return super().dispatch(request, *args, **kwargs)


class ImproperlyConfiguredPermission(Exception):
    """Una vista protegida no declaro que permiso exige."""


class StoreScopedQuerysetMixin:
    """Restringe el queryset de una vista a las tiendas del usuario.

    Se aplica SIEMPRE, incluso para consultas por clave primaria, de modo que
    un objeto de otra tienda produzca 404 en vez de 403: no revelamos siquiera
    que el identificador existe.
    """

    def get_queryset(self):
        queryset = super().get_queryset()  # type: ignore[misc]
        request = self.request  # type: ignore[attr-defined]
        membership = get_current_membership(request)
        if request.user.is_platform_admin:
            return queryset
        if membership is None:
            return queryset.none()
        return queryset.filter(store_id=membership.store_id)


# ---------------------------------------------------------------------------
# Permisos para Django REST Framework
# ---------------------------------------------------------------------------


class HasStorePermission(drf_permissions.BasePermission):
    """Permiso DRF que exige un permiso declarado en la vista."""

    message = "Tu rol no tiene acceso a esta operacion."

    def has_permission(self, request, view) -> bool:
        permission = getattr(view, "required_permission", None)
        if permission is None:
            # Denegar por defecto: una vista de API sin permiso declarado no
            # se sirve. Esto convierte un olvido en un fallo visible.
            return False
        return user_has_perm(request, permission)

    def has_object_permission(self, request, view, obj) -> bool:
        """Segunda barrera: el objeto debe pertenecer a la tienda activa."""
        if request.user.is_platform_admin:
            return True
        membership = get_current_membership(request)
        if membership is None:
            return False
        store_id = getattr(obj, "store_id", None)
        if store_id is None:
            return False
        return str(store_id) == str(membership.store_id)


class IsPlatformAdmin(drf_permissions.BasePermission):
    """Solo administradores de SAMY Cloud."""

    message = "Requiere permisos de administrador de plataforma."

    def has_permission(self, request, view) -> bool:
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_platform_admin
        )


class IsStoreOwner(drf_permissions.BasePermission):
    """Dueno de la tienda activa (o administrador de plataforma)."""

    message = "Requiere ser propietario del negocio."

    def has_permission(self, request, view) -> bool:
        if not request.user.is_authenticated:
            return False
        if request.user.is_platform_admin:
            return True
        membership = get_current_membership(request)
        return bool(membership and membership.role == Role.STORE_OWNER)
