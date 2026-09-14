"""API de recargas.

``create_topup`` NO recarga: solo registra la intencion y devuelve el
identificador. La ejecucion ocurre despues, cuando llega el evento de pago
confirmado. Es la regla del dinero expresada en la forma de la API: no existe
un endpoint que recargue directamente.
"""

from __future__ import annotations

import structlog
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from samy_common.providers.exceptions import (
    ProviderError,
    ProviderIndeterminateError,
)
from samy_common.states import FulfillmentState

log = structlog.get_logger("fulfillment.api")

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


@extend_schema(
    responses={200: TopupFulfillmentSerializer},
    description=(
        "Ejecuta AHORA una recarga cuya orden ya esta pagada. No es un atajo "
        "para recargar: execute_topup vuelve a preguntarle al servicio de "
        "Pagos si la orden esta pagada y se niega si no lo esta."
    ),
)
@api_view(["POST"])
@idempotent(scope="topups:execute")
def ejecutar_recarga(request: Request, fulfillment_id: str) -> Response:
    """Ejecucion sincrona de una recarga ya pagada.

    POR QUE EXISTE, SI YA HAY UN WORKER
    -----------------------------------
    El camino normal es asincrono: el evento de pago confirmado llega por el
    outbox y un worker de Celery llama a ``execute_topup``. Eso esta bien en un
    servidor que no se apaga.

    En Cloud Run la instancia se apaga cuando no hay trafico, y un worker que
    desaparece entre "cobre el efectivo" y "manda la recarga" deja al cliente
    pagado y sin servicio, sin nadie mirando. Este endpoint hace que la
    ejecucion ocurra DENTRO de la peticion en la que una persona autorizada
    confirmo la recarga: mientras esa peticion vive, el contenedor vive.

    QUE NO CAMBIA
    -------------
    Ni una de las guardas. ``execute_topup`` sigue verificando el pago contra
    el servicio de Pagos, sigue exigiendo ``ensure_ready()`` -ambiente,
    credenciales y la bandera de dinero- y sigue tomando ``select_for_update``
    sobre la fila antes de pasar a SENT. Dos peticiones simultaneas no pueden
    mandar dos recargas: la segunda encuentra el estado ya movido.

    Y el estado vive en PostgreSQL, no en Redis ni en memoria: si Cloud Run
    reinicia, una recarga SUCCEEDED sigue siendo SUCCEEDED y este endpoint la
    devuelve tal cual sin volver a llamar al proveedor.
    """
    from django.shortcuts import get_object_or_404

    queryset = TopupFulfillment.objects.select_related("product", "product__operator")
    # Misma barrera que en la consulta: una tienda no toca las recargas de
    # otra, y el fallo es 404 y no 403 para no confirmar que el id existe.
    if store_id := (request.data or {}).get("store_id"):
        queryset = queryset.filter(store_id=store_id)

    fulfillment = get_object_or_404(queryset, pk=fulfillment_id)

    # Terminal: se responde el estado guardado y NO se llama al proveedor.
    # Es la defensa contra el doble clic y contra el reintento de Cloud Run, y
    # esta antes de cualquier otra cosa a proposito.
    if fulfillment.state_enum in {
        FulfillmentState.SUCCEEDED,
        FulfillmentState.FAILED,
        FulfillmentState.REVERSED,
        FulfillmentState.UNDER_REVIEW,
        FulfillmentState.SENT,
    }:
        return Response(TopupFulfillmentSerializer(fulfillment).data)

    try:
        fulfillment = services.execute_topup(fulfillment=fulfillment)
    except ProviderIndeterminateError as exc:
        # No se sabe si la recarga se aplico. NO se reintenta: se deja para
        # revision humana, que es lo que pidio el dueno del saldo.
        log.error(
            "topup_execution_indeterminate",
            fulfillment_id=str(fulfillment.id),
            error=exc.message,
        )
        fulfillment.refresh_from_db()
        return Response(
            {
                "error": {
                    "code": "topup_indeterminate",
                    "message": exc.message,
                },
                "topup": TopupFulfillmentSerializer(fulfillment).data,
            },
            status=status.HTTP_409_CONFLICT,
        )
    except ProviderError as exc:
        log.warning(
            "topup_execution_failed",
            fulfillment_id=str(fulfillment.id),
            error=exc.message,
        )
        fulfillment.refresh_from_db()
        return Response(
            {
                "error": {"code": exc.code, "message": exc.message},
                "topup": TopupFulfillmentSerializer(fulfillment).data,
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return Response(TopupFulfillmentSerializer(fulfillment).data)
