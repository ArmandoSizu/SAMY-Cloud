"""API de servicios pagables y consulta de adeudo."""

from __future__ import annotations

import structlog
from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.idempotency import idempotent
from apps.billers.models import Biller
from apps.billers.serializers import BillerSerializer, InquirySerializer
from apps.fulfillment.models import BillInquiryLog
from apps.providers.registry import get_provider
from samy_common.money import Money
from samy_common.providers.exceptions import ProviderError
from samy_common.security.masking import mask_reference

log = structlog.get_logger("billers.api")


@extend_schema(
    responses={200: BillerSerializer(many=True)},
    description=(
        "Servicios pagables. Vacio y explicado si no hay agregador contratado: "
        "CFE y los organismos de agua no exponen API publica."
    ),
)
@api_view(["GET"])
def billers(request: Request) -> Response:
    queryset = (
        Biller.objects.filter(is_active=True)
        .select_related("reference_format")
        .order_by("display_order", "name")
    )
    data = BillerSerializer(queryset, many=True).data

    if not data:
        health = get_provider().check_health()
        return Response(
            {
                "billers": [],
                "available": False,
                "reason": health.detail,
                "provider_status": str(health.status),
                "missing_requirements": list(health.missing_requirements),
            }
        )

    return Response({"billers": data, "available": True})


@extend_schema(
    request=InquirySerializer,
    description=(
        "Consulta el adeudo de un recibo. No cobra nada. La validacion del "
        "formato es solo de usabilidad; la que cuenta es la respuesta del "
        "agregador."
    ),
)
@api_view(["POST"])
@idempotent(scope="billers:inquire")
def inquire(request: Request) -> Response:
    serializer = InquirySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    biller = (
        Biller.objects.select_related("reference_format")
        .filter(pk=data["biller_id"], is_active=True)
        .first()
    )
    if biller is None:
        return Response(
            {"error": {"code": "biller_not_found", "message": "Ese servicio no esta disponible."}},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Validacion de formato: atrapa errores de captura antes de molestar al
    # agregador. NO sustituye a su validacion.
    fmt = getattr(biller, "reference_format", None)
    reference = data["reference"].strip()
    if fmt is not None:
        try:
            reference = fmt.validate(reference)
        except ValidationError as exc:
            return Response(
                {"error": {"code": "invalid_reference", "message": exc.messages[0]}},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    provider = get_provider()
    provider.ensure_ready()

    try:
        inquiry = provider.inquire(
            biller_id=biller.provider_biller_id, reference=reference
        )
    except ProviderError:
        raise

    BillInquiryLog.objects.create(
        biller=biller,
        store_id=data["store_id"],
        requested_by_id=data.get("requested_by_id"),
        reference_masked=mask_reference(reference),
        found=inquiry.found,
        amount_cents=inquiry.amount_due.cents if inquiry.amount_due else None,
        source=data["source"],
    )

    return Response(
        {
            "found": inquiry.found,
            "reference": inquiry.reference,
            "biller_id": str(biller.id),
            "biller_name": biller.name,
            "amount_due_cents": inquiry.amount_due.cents if inquiry.amount_due else None,
            "amount_due_display": str(inquiry.amount_due) if inquiry.amount_due else "",
            "due_date": inquiry.due_date.isoformat() if inquiry.due_date else None,
            "customer_name": inquiry.customer_name,
            "period": inquiry.period,
            "is_overdue": inquiry.is_overdue,
            "allows_partial": inquiry.allows_partial,
            "message": inquiry.message,
        }
    )
