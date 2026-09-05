"""API de comisiones.

``preview`` la usa el Core para mostrar el desglose ANTES de cobrar. Es
importante que el calculo lo haga este servicio y no el navegador: dos
calculos del mismo dinero en dos lugares distintos terminan divergiendo, y el
cliente veria un total distinto al que se le cobra.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.commissions import engine
from apps.commissions.models import CommissionRule, ServiceKind
from samy_common.money import Money


class PreviewSerializer(serializers.Serializer):
    store_id = serializers.UUIDField()
    organization_id = serializers.UUIDField(required=False, allow_null=True)
    service_kind = serializers.ChoiceField(choices=ServiceKind.choices)
    product_code = serializers.CharField(required=False, allow_blank=True, max_length=64)
    base_cents = serializers.IntegerField(min_value=0)


@extend_schema(
    request=PreviewSerializer,
    description=(
        "Calcula la comision de una operacion sin crear nada. Es lo que el "
        "cajero ve en la pantalla de confirmacion."
    ),
)
@api_view(["POST"])
def preview(request: Request) -> Response:
    serializer = PreviewSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    result = engine.calculate(
        base=Money(data["base_cents"]),
        service_kind=data["service_kind"],
        store_id=data["store_id"],
        organization_id=data.get("organization_id"),
        product_code=data.get("product_code", ""),
    )

    payload = result.as_dict()
    payload["base_display"] = str(result.base)
    payload["commission_display"] = str(result.commission)
    payload["total_display"] = str(result.total)
    return Response(payload)


@extend_schema(description="Reglas de comision configuradas para una tienda.")
@api_view(["GET"])
def rules(request: Request) -> Response:
    store_id = request.query_params.get("store_id")
    if not store_id:
        return Response(
            {"error": {"code": "missing_store_id", "message": "Falta store_id."}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    queryset = CommissionRule.objects.filter(is_active=True).select_related("split")
    applicable = [
        r
        for r in queryset
        if r.store_id is None or str(r.store_id) == str(store_id)
    ]

    return Response(
        {
            "rules": [
                {
                    "id": str(rule.id),
                    "name": rule.name,
                    "service_kind": rule.service_kind,
                    "commission_type": rule.commission_type,
                    "description": rule.describe(),
                    "scope": (
                        "tienda" if rule.store_id
                        else "organizacion" if rule.organization_id
                        else "plataforma"
                    ),
                    "split": (
                        str(rule.split) if hasattr(rule, "split") and rule.split else None
                    ),
                    "is_currently_valid": rule.is_currently_valid,
                }
                for rule in sorted(applicable, key=lambda r: -r.specificity)
            ]
        }
    )
