"""API del catalogo de recargas.

Cuando el catalogo esta vacio la respuesta lo dice explicitamente y explica por
que, en vez de devolver una lista vacia sin contexto. La diferencia importa: el
cajero tiene que poder distinguir "no hay productos" de "falta configurar el
proveedor", y el dueno tiene que saber que hacer al respecto.
"""

from __future__ import annotations

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.catalog.models import CatalogSyncRun, Operator, TopupProduct
from apps.catalog.serializers import OperatorSerializer
from apps.catalog.sync import catalog_is_stale
from apps.providers.registry import get_provider


@extend_schema(
    responses={200: OperatorSerializer(many=True)},
    description="Operadores y denominaciones REALES disponibles del proveedor.",
)
@api_view(["GET"])
def catalog(request: Request) -> Response:
    operators = (
        Operator.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch(
                "products",
                queryset=TopupProduct.objects.filter(is_active=True).order_by(
                    "amount_cents", "label"
                ),
            )
        )
        .order_by("display_order", "name")
    )

    data = OperatorSerializer(operators, many=True).data

    if not data:
        # Catalogo vacio: se explica la causa concreta consultando la salud
        # del proveedor, para que la UI muestre algo accionable.
        health = get_provider().check_health()
        return Response(
            {
                "operators": [],
                "available": False,
                "reason": health.detail,
                "provider_status": str(health.status),
                "missing_requirements": list(health.missing_requirements),
            }
        )

    last_sync = (
        CatalogSyncRun.objects.filter(succeeded=True).order_by("-finished_at").first()
    )
    return Response(
        {
            "operators": data,
            "available": True,
            "is_stale": catalog_is_stale(),
            "last_synced_at": last_sync.finished_at.isoformat() if last_sync else None,
            "provider_mode": last_sync.provider_mode if last_sync else "",
        }
    )
