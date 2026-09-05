"""API de ordenes.

Solo la consume el Core, con peticiones firmadas. Cada endpoint que muta algo
exige ``Idempotency-Key``: sin ella la peticion se rechaza con 400, en vez de
arriesgar un doble cobro.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import structlog
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.idempotency import idempotent
from apps.orders import services
from apps.orders.models import Order
from apps.orders.serializers import (
    ConfirmCashSerializer,
    CreateOrderSerializer,
    OrderDetailSerializer,
    OrderSummarySerializer,
    StartPaymentSerializer,
)
from apps.providers.registry import get_provider
from samy_common.money import Money
from samy_common.states import OrderState

log = structlog.get_logger("orders.api")


@extend_schema(
    request=CreateOrderSerializer,
    responses={201: OrderDetailSerializer},
    description=(
        "Crea una orden con su comision calculada. Requiere Idempotency-Key: "
        "dos peticiones con la misma clave devuelven la misma orden."
    ),
)
@api_view(["POST"])
@idempotent(scope="orders:create")
def create_order(request: Request) -> Response:
    serializer = CreateOrderSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    order = services.create_order(
        organization_id=data["organization_id"],
        store_id=data["store_id"],
        store_code=data["store_code"],
        created_by_id=data["created_by_id"],
        created_by_email=data.get("created_by_email", ""),
        service_kind=data["service_kind"],
        description=data["description"],
        base_amount=Money(data["base_cents"], data.get("currency", "MXN")),
        product_code=data.get("product_code", ""),
        fulfillment_id=data.get("fulfillment_id"),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
        correlation_id=getattr(request, "correlation_id", ""),
        metadata=data.get("metadata") or {},
    )

    return Response(
        OrderDetailSerializer(order).data,
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    request=StartPaymentSerializer,
    responses={200: OrderDetailSerializer},
    description=(
        "Inicia el cobro con el proveedor del metodo indicado. Si el proveedor "
        "no tiene credenciales, responde 503 y la orden NO avanza de estado."
    ),
)
@api_view(["POST"])
@idempotent(scope="orders:start_payment")
def start_payment(request: Request, order_id: str) -> Response:
    serializer = StartPaymentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    order = _get_order(order_id, store_id=serializer.validated_data["store_id"])

    order, attempt = services.start_payment(
        order=order,
        method=serializer.validated_data["method"],
        actor_id=serializer.validated_data["actor_id"],
        card_token=serializer.validated_data.get("card_token", ""),
    )

    payload = OrderDetailSerializer(order).data
    payload["attempt"] = {
        "id": str(attempt.id),
        "status": attempt.status,
        "provider": attempt.provider_slug,
        "provider_mode": attempt.provider_mode,
        "checkout_url": attempt.checkout_url,
        "expires_at": attempt.expires_at.isoformat() if attempt.expires_at else None,
    }
    return Response(payload)


@extend_schema(
    request=ConfirmCashSerializer,
    responses={200: OrderDetailSerializer},
    description=(
        "Confirma la recepcion de efectivo en el mostrador. Exige registrar "
        "cuanto entrego el cliente; el sistema calcula el cambio y rechaza "
        "montos insuficientes."
    ),
)
@api_view(["POST"])
@idempotent(scope="orders:confirm_cash")
def confirm_cash(request: Request, order_id: str) -> Response:
    serializer = ConfirmCashSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    order = _get_order(order_id, store_id=data["store_id"])

    attempt = order.attempts.filter(provider_slug="cash").order_by("-created_at").first()
    if attempt is None:
        return Response(
            {
                "error": {
                    "code": "no_cash_attempt",
                    "message": "Esta orden no tiene un cobro en efectivo iniciado.",
                }
            },
            status=status.HTTP_409_CONFLICT,
        )

    provider = get_provider("cash")
    result, change = provider.confirm_cash_received(  # type: ignore[attr-defined]
        intent_reference=attempt.provider_reference,
        amount_due=order.total,
        amount_tendered=Money(data["amount_tendered_cents"], order.currency),
    )

    order = services.confirm_payment(
        order=order,
        attempt=attempt,
        provider_reference=result.provider_reference,
        source="efectivo_confirmado_por_cajero",
        actor_id=data["actor_id"],
        amount_received=Money(data["amount_tendered_cents"], order.currency),
    )

    payload = OrderDetailSerializer(order).data
    payload["change_cents"] = change.cents
    payload["change_display"] = str(change)
    return Response(payload)


@extend_schema(responses={200: OrderDetailSerializer})
@api_view(["GET"])
def order_detail(request: Request, order_id: str) -> Response:
    store_id = request.query_params.get("store_id")
    order = _get_order(order_id, store_id=store_id)
    return Response(OrderDetailSerializer(order).data)


@extend_schema(
    parameters=[
        OpenApiParameter("store_id", str, required=True),
        OpenApiParameter("scope", str, description="'store' o 'own'"),
        OpenApiParameter("user_id", str, description="Requerido si scope=own"),
    ],
    responses={200: OrderSummarySerializer},
    description=(
        "Resumen del dia. Devuelve conteos y montos REALES; si no hubo "
        "operaciones devuelve ceros explicitos, nunca datos de relleno."
    ),
)
@api_view(["GET"])
def daily_summary(request: Request) -> Response:
    store_id = request.query_params.get("store_id")
    if not store_id:
        return Response(
            {"error": {"code": "missing_store_id", "message": "Falta store_id."}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    queryset = Order.objects.for_store(store_id).today()
    if request.query_params.get("scope") == "own":
        user_id = request.query_params.get("user_id")
        if not user_id:
            return Response(
                {
                    "error": {
                        "code": "missing_user_id",
                        "message": "scope=own requiere user_id.",
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(created_by_id=user_id)

    aggregates = queryset.aggregate(
        total_operations=Count("id"),
        successful=Count("id", filter=Q(state=OrderState.SUCCESS)),
        failed=Count("id", filter=Q(state__in=[OrderState.FAILED, OrderState.EXPIRED])),
        pending=Count(
            "id",
            filter=Q(
                state__in=[
                    OrderState.CREATED,
                    OrderState.PAYMENT_PENDING,
                    OrderState.PAID,
                    OrderState.PROCESSING,
                ]
            ),
        ),
        under_review=Count("id", filter=Q(state=OrderState.UNDER_REVIEW)),
        gross_cents=Sum("total_cents", filter=Q(state=OrderState.SUCCESS)),
        commission_cents=Sum("commission_cents", filter=Q(state=OrderState.SUCCESS)),
    )

    store_share = (
        queryset.filter(state=OrderState.SUCCESS).aggregate(
            total=Sum("commission_entry__store_share_cents")
        )["total"]
        or 0
    )

    return Response(
        {
            "date": timezone.localdate().isoformat(),
            "total_operations": aggregates["total_operations"] or 0,
            "successful": aggregates["successful"] or 0,
            "failed": aggregates["failed"] or 0,
            "pending": aggregates["pending"] or 0,
            "under_review": aggregates["under_review"] or 0,
            "gross_cents": aggregates["gross_cents"] or 0,
            "commission_cents": aggregates["commission_cents"] or 0,
            "store_earnings_cents": store_share,
            "currency": "MXN",
        }
    )


def _get_order(order_id: str, store_id: str | None = None) -> Order:
    """Obtiene una orden, siempre acotada a su tienda.

    Filtrar por ``store_id`` aunque el id sea un UUID no es redundante: es la
    barrera que impide que una tienda consulte la orden de otra si alguna vez
    un identificador se filtra. Devuelve 404, no 403: no confirmamos ni
    siquiera que el identificador exista.
    """
    from django.shortcuts import get_object_or_404

    queryset = Order.objects.select_related("commission_entry")
    if store_id:
        queryset = queryset.filter(store_id=store_id)
    return get_object_or_404(queryset, pk=order_id)


@extend_schema(
    parameters=[
        OpenApiParameter("store_id", str, required=True),
        OpenApiParameter("scope", str, description="'store' o 'own'"),
        OpenApiParameter("user_id", str, description="Requerido si scope=own"),
        OpenApiParameter("state", str, description="Filtra por estado"),
    ],
    responses={200: OrderDetailSerializer(many=True)},
    description="Historial de operaciones, siempre acotado a una tienda.",
)
@api_view(["GET"])
def order_history(request: Request) -> Response:
    store_id = request.query_params.get("store_id")
    if not store_id:
        return Response(
            {"error": {"code": "missing_store_id", "message": "Falta store_id."}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    queryset = Order.objects.for_store(store_id).select_related("commission_entry")

    # El alcance lo decide quien llama (el Core, que ya resolvio el permiso).
    # Aqui se aplica tal cual: este servicio no conoce roles.
    if request.query_params.get("scope") == "own":
        user_id = request.query_params.get("user_id")
        if not user_id:
            return Response(
                {"error": {"code": "missing_user_id", "message": "scope=own requiere user_id."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(created_by_id=user_id)

    if state := request.query_params.get("state"):
        queryset = queryset.filter(state=state)

    try:
        limit = min(int(request.query_params.get("limit", 50)), 200)
    except ValueError:
        limit = 50

    orders = queryset.order_by("-created_at")[:limit]
    return Response(
        {
            "results": OrderDetailSerializer(orders, many=True).data,
            "count": len(orders),
        }
    )
