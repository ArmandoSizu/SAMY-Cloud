"""API de recargas.

``create_topup`` NO recarga: solo registra la intencion y devuelve el
identificador. La ejecucion ocurre despues, cuando llega el evento de pago
confirmado. Es la regla del dinero expresada en la forma de la API: no existe
un endpoint que recargue directamente.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.idempotency import idempotent
from apps.fulfillment import services
from apps.fulfillment.models import TopupFulfillment
from apps.fulfillment.serializers import (
    CreateTopupSerializer,
    TopupFulfillmentSerializer,
)
from samy_common.money import Money


@extend_schema(
    request=CreateTopupSerializer,
    responses={201: TopupFulfillmentSerializer},
    description=(
        "Registra una recarga en estado PENDING_PAYMENT. No ejecuta nada: la "
        "recarga se envia al operador solo cuando la orden queda PAGADA."
    ),
)
@api_view(["POST"])
@idempotent(scope="topups:create")
def create_topup(request: Request) -> Response:
    serializer = CreateTopupSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    amount = (
        Money(data["amount_cents"]) if data.get("amount_cents") is not None else None
    )

    fulfillment = services.create_fulfillment(
        organization_id=data["organization_id"],
        store_id=data["store_id"],
        requested_by_id=data["requested_by_id"],
        product_id=data["product_id"],
        phone_raw=data["phone"],
        amount=amount,
        correlation_id=getattr(request, "correlation_id", ""),
    )

    return Response(
        TopupFulfillmentSerializer(fulfillment).data,
        status=status.HTTP_201_CREATED,
    )


@extend_schema(responses={200: TopupFulfillmentSerializer})
@api_view(["GET"])
def topup_detail(request: Request, fulfillment_id: str) -> Response:
    from django.shortcuts import get_object_or_404

    queryset = TopupFulfillment.objects.select_related("product", "product__operator")
    # Acotar por tienda aunque el id sea UUID: es la barrera que impide que
    # una tienda consulte la recarga de otra. 404, no 403.
    if store_id := request.query_params.get("store_id"):
        queryset = queryset.filter(store_id=store_id)

    fulfillment = get_object_or_404(queryset, pk=fulfillment_id)
    return Response(TopupFulfillmentSerializer(fulfillment).data)
