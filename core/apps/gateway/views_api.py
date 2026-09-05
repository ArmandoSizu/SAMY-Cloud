"""API JSON del Core Platform.

Deliberadamente minima. El Core sirve HTML al navegador; esta API existe para
una futura app movil y para que el panel de plataforma consulte el estado de
las integraciones sin duplicar logica.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.gateway.clients import billpay_client, payments_client, topups_client
from apps.tenancy.permissions import IsPlatformAdmin
from samy_common.providers.exceptions import ProviderError


@extend_schema(description="Usuario autenticado y su contexto de tienda actual.")
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request: Request) -> Response:
    membership = getattr(request, "membership", None)
    return Response(
        {
            "id": str(request.user.id),
            "email": request.user.email,
            "full_name": request.user.full_name,
            "initials": request.user.initials,
            "is_platform_admin": request.user.is_platform_admin,
            "current_store": (
                {
                    "id": str(request.store.id),
                    "name": request.store.name,
                    "code": request.store.code,
                }
                if request.store
                else None
            ),
            "role": membership.role if membership else None,
            "permissions": sorted(membership.permissions) if membership else [],
        }
    )


@extend_schema(description="Tiendas donde el usuario tiene membresia activa.")
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_stores(request: Request) -> Response:
    return Response(
        {
            "stores": [
                {
                    "id": str(m.store_id),
                    "name": m.store.name,
                    "code": m.store.code,
                    "role": m.role,
                    "is_default": m.is_default,
                    "organization": m.store.organization.name,
                }
                for m in request.user.active_memberships()
            ]
        }
    )


@extend_schema(
    description=(
        "Estado real de todas las integraciones externas. Solo administradores "
        "de plataforma: revela topologia interna."
    )
)
@api_view(["GET"])
@permission_classes([IsPlatformAdmin])
def providers_status(request: Request) -> Response:
    result: dict[str, object] = {}
    for name, client in (
        ("payments", payments_client()),
        ("topups", topups_client()),
        ("billpay", billpay_client()),
    ):
        try:
            data = client.get("/api/v1/providers/").data or {}
            result[name] = {"reachable": True, "providers": data.get("providers", [])}
        except ProviderError as exc:
            result[name] = {"reachable": False, "error": exc.message, "providers": []}
    return Response({"services": result})
