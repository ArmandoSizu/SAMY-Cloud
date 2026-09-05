"""Estado de las integraciones de pago."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.providers.registry import describe_all


@extend_schema(
    description=(
        "Estado real de cada adaptador de pago: si tiene credenciales "
        "verificadas, que le falta, y si puede operar. El estado READY solo "
        "se devuelve tras comprobarlo contra el proveedor, no por tener la "
        "variable de entorno llena."
    )
)
@api_view(["GET"])
def providers_status(request: Request) -> Response:
    return Response({"providers": describe_all()})
