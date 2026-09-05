"""Estado de las integraciones de recargas, para el panel de plataforma."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.providers.registry import describe_all


@extend_schema(
    description=(
        "Estado real de cada adaptador de recargas: si tiene credenciales, "
        "que le falta y si puede operar."
    )
)
@api_view(["GET"])
def providers_status(request: Request) -> Response:
    return Response({"providers": describe_all()})
