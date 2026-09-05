"""API de pagos de recibos.

``create_payment`` NO paga: registra la intencion. El pago se ejecuta cuando
llega el evento de orden pagada y se verifica contra el servicio de Pagos.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.idempotency import idempotent
from apps.fulfillment import services
from apps.fulfillment.models import BillPaymentFulfillment
from apps.fulfillment.serializers import (
    BillPaymentSerializer,
    CreateBillPaymentSerializer,
)
from samy_common.money import Money


@extend_schema(
    request=CreateBillPaymentSerializer,
    responses={201: BillPaymentSerializer},
    description=(
        "Registra el pago de un recibo en estado PENDING_PAYMENT. No paga "
        "nada: el recibo se paga solo cuando la orden queda PAGADA."
    ),
)
@api_view(["POST"])
@idempotent(scope="billpay:create")
def create_payment(request: Request) -> Response:
    serializer = CreateBillPaymentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    fulfillment = services.create_fulfillment(
        organization_id=data["organization_id"],
        store_id=data["store_id"],
        requested_by_id=data["requested_by_id"],
        biller_id=data["biller_id"],
        reference=data["reference"],
        amount=Money(data["amount_cents"]),
        customer_name=data.get("customer_name", ""),
        period=data.get("period", ""),
        correlation_id=getattr(request, "correlation_id", ""),
    )

    return Response(
        BillPaymentSerializer(fulfillment).data, status=status.HTTP_201_CREATED
    )


@extend_schema(responses={200: BillPaymentSerializer})
@api_view(["GET"])
def payment_detail(request: Request, fulfillment_id: str) -> Response:
    from django.shortcuts import get_object_or_404

    queryset = BillPaymentFulfillment.objects.select_related("biller")
    if store_id := request.query_params.get("store_id"):
        queryset = queryset.filter(store_id=store_id)

    fulfillment = get_object_or_404(queryset, pk=fulfillment_id)
    return Response(BillPaymentSerializer(fulfillment).data)
