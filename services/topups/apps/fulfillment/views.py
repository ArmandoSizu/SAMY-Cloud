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
    LinkOrderSerializer,
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


@extend_schema(
    request=LinkOrderSerializer,
    responses={200: TopupFulfillmentSerializer},
    description=(
        "Enlaza una recarga con la orden que la cobra. Sin este enlace la "
        "recarga NUNCA se ejecuta: execute_topup exige una orden para poder "
        "verificar que el pago se confirmo."
    ),
)
@api_view(["POST"])
def link_order(request: Request, fulfillment_id: str) -> Response:
    """Ata la recarga a su orden.

    Hace falta porque las dos entidades viven en bases de datos distintas y se
    crean en dos llamadas: primero la recarga, despues la orden que la paga.
    La orden nace sabiendo a que recarga corresponde; esta llamada cierra el
    circulo en el otro sentido.

    Sin ella, ``execute_topup`` rechaza la ejecucion por no tener orden con la
    que comprobar el pago: la recarga se quedaria pagada y sin entregar.

    Dos guardas:

    * Se acota por tienda, asi que una tienda no puede enganchar su orden a la
      recarga de otra.
    * El enlace es de una sola vez. Reapuntar una recarga ya enlazada a otra
      orden permitiria pagar una recarga barata y entregar una cara.
    """
    from django.shortcuts import get_object_or_404

    serializer = LinkOrderSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    fulfillment = get_object_or_404(
        TopupFulfillment.objects.filter(store_id=data["store_id"]),
        pk=fulfillment_id,
    )

    if fulfillment.order_id and fulfillment.order_id != data["order_id"]:
        return Response(
            {
                "detail": (
                    "Esta recarga ya esta enlazada a otra orden. No se puede "
                    "reapuntar."
                )
            },
            status=status.HTTP_409_CONFLICT,
        )

    if not fulfillment.order_id:
        fulfillment.order_id = data["order_id"]
        fulfillment.save(update_fields=["order_id", "updated_at"])

    return Response(TopupFulfillmentSerializer(fulfillment).data)


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
