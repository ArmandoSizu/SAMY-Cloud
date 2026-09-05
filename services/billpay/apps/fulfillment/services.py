"""Ejecucion de pagos de recibos.

Misma regla que en recargas: ``execute_payment()`` REHUSA ejecutar si la orden
no esta pagada, y no confia en el evento: vuelve a preguntarle al servicio de
Pagos por el estado real antes de gastar saldo del agregador.
"""

from __future__ import annotations

import uuid

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.billers.models import Biller
from apps.fulfillment.models import BillPaymentFulfillment
from apps.outbox.models import OutboxEvent
from apps.providers.base import BillPaymentRequest, BillPaymentStatus
from apps.providers.registry import get_provider
from samy_common.http.client import ServiceClient, ServiceClientConfig
from samy_common.money import Money
from samy_common.providers.exceptions import (
    ProviderError,
    ProviderIndeterminateError,
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.security.masking import mask_reference
from samy_common.states import FulfillmentState

log = structlog.get_logger("billpay.services")


@transaction.atomic
def create_fulfillment(
    *,
    organization_id: uuid.UUID | str,
    store_id: uuid.UUID | str,
    requested_by_id: uuid.UUID | str,
    biller_id: uuid.UUID | str,
    reference: str,
    amount: Money,
    customer_name: str = "",
    period: str = "",
    correlation_id: str = "",
) -> BillPaymentFulfillment:
    """Registra el pago de un recibo. Todavia NO paga nada."""
    biller = (
        Biller.objects.select_related("reference_format")
        .filter(pk=biller_id, is_active=True)
        .first()
    )
    if biller is None:
        raise ValidationError("Ese servicio ya no esta disponible.")

    fmt = getattr(biller, "reference_format", None)
    clean_reference = fmt.validate(reference) if fmt else reference.strip()

    if amount.cents <= 0:
        raise ValidationError("El monto a pagar debe ser mayor a cero.")
    if biller.min_amount_cents and amount.cents < biller.min_amount_cents:
        raise ValidationError(
            f"El monto minimo para {biller.name} es "
            f"{Money(biller.min_amount_cents)}."
        )
    if biller.max_amount_cents and amount.cents > biller.max_amount_cents:
        raise ValidationError(
            f"El monto maximo para {biller.name} es "
            f"{Money(biller.max_amount_cents)}."
        )

    fulfillment = BillPaymentFulfillment.objects.create(
        organization_id=organization_id,
        store_id=store_id,
        requested_by_id=requested_by_id,
        biller=biller,
        biller_name=biller.name,
        reference=clean_reference,
        reference_masked=mask_reference(clean_reference),
        currency=amount.currency,
        amount_cents=amount.cents,
        customer_name=customer_name[:160],
        period=period[:60],
        state=FulfillmentState.PENDING_PAYMENT,
        provider_slug=biller.provider_slug,
        idempotency_key=f"billpay:{uuid.uuid4().hex}",
        correlation_id=correlation_id,
    )

    log.info(
        "billpay_fulfillment_created",
        fulfillment_id=str(fulfillment.id),
        biller=biller.name,
        reference_masked=fulfillment.reference_masked,
        amount_cents=amount.cents,
    )
    return fulfillment


def execute_payment(*, fulfillment: BillPaymentFulfillment) -> BillPaymentFulfillment:
    """Paga el recibo. Solo con la orden confirmada como pagada."""
    if fulfillment.state_enum not in {
        FulfillmentState.QUEUED,
        FulfillmentState.PENDING_PAYMENT,
    }:
        log.info(
            "billpay_execution_skipped",
            fulfillment_id=str(fulfillment.id),
            state=fulfillment.state,
        )
        return fulfillment

    if not fulfillment.order_id:
        raise ProviderPermanentError(
            provider="billpay",
            message="El pago no tiene orden asociada; no se puede ejecutar.",
        )

    if not _order_is_paid(fulfillment.order_id):
        log.error(
            "billpay_execution_blocked_order_not_paid",
            fulfillment_id=str(fulfillment.id),
            order_id=str(fulfillment.order_id),
        )
        raise ProviderPermanentError(
            provider="billpay",
            message="La orden asociada no esta pagada. El recibo no se paga.",
        )

    if fulfillment.state_enum == FulfillmentState.PENDING_PAYMENT:
        fulfillment = fulfillment.transition(
            FulfillmentState.QUEUED, reason="Pago confirmado."
        )

    provider = get_provider(fulfillment.provider_slug or None)
    provider.ensure_ready()

    fulfillment.attempts += 1
    fulfillment.provider_mode = str(provider.mode)
    fulfillment.save(update_fields=["attempts", "provider_mode", "updated_at"])

    fulfillment = fulfillment.transition(
        FulfillmentState.SENT, reason=f"Enviado a {provider.display_name}."
    )

    request = BillPaymentRequest(
        fulfillment_id=fulfillment.id,
        order_id=fulfillment.order_id,
        biller_id=fulfillment.biller.provider_biller_id,
        reference=fulfillment.reference,
        amount=fulfillment.amount,
        idempotency_key=fulfillment.idempotency_key,
    )

    try:
        result = provider.pay(request)
    except ProviderIndeterminateError as exc:
        # No sabemos si se pago. NUNCA se reintenta a ciegas: pagar dos veces
        # el mismo recibo es dinero que no se recupera.
        fulfillment.failure_reason = exc.message[:255]
        fulfillment.save(update_fields=["failure_reason", "updated_at"])
        fulfillment.transition(
            FulfillmentState.UNDER_REVIEW,
            reason="Respuesta indeterminada del agregador.",
        )
        _publish_result(fulfillment, succeeded=False, pending_review=True)
        raise
    except (ProviderPermanentError, ProviderTransientError) as exc:
        fulfillment.failure_reason = exc.message[:255]
        fulfillment.save(update_fields=["failure_reason", "updated_at"])
        fulfillment.transition(FulfillmentState.FAILED, reason=exc.message)
        _publish_result(fulfillment, succeeded=False)
        raise

    fulfillment.provider_reference = result.provider_reference[:128]
    fulfillment.biller_reference = result.biller_reference[:128]
    fulfillment.raw_response = result.raw_response
    fulfillment.save(
        update_fields=["provider_reference", "biller_reference", "raw_response", "updated_at"]
    )

    if result.status == BillPaymentStatus.SUCCEEDED:
        fulfillment = fulfillment.transition(
            FulfillmentState.SUCCEEDED, reason="El servicio confirmo el pago."
        )
        _publish_result(fulfillment, succeeded=True)
    elif result.status == BillPaymentStatus.FAILED:
        fulfillment.failure_reason = (
            result.failure_reason or "Rechazado por el servicio."
        )[:255]
        fulfillment.save(update_fields=["failure_reason", "updated_at"])
        fulfillment = fulfillment.transition(
            FulfillmentState.FAILED, reason=fulfillment.failure_reason
        )
        _publish_result(fulfillment, succeeded=False)
    else:
        log.info(
            "billpay_pending_at_provider",
            fulfillment_id=str(fulfillment.id),
            status=result.status,
        )

    return fulfillment


def _order_is_paid(order_id: uuid.UUID) -> bool:
    """Pregunta al servicio de Pagos. Ante cualquier duda, False."""
    client = ServiceClient(
        ServiceClientConfig(
            base_url=settings.SERVICE_URLS["payments"],
            secret=settings.SERVICE_S2S_SECRET,
            caller=settings.SERVICE_NAME,
        )
    )
    try:
        response = client.get(f"/api/v1/orders/{order_id}/")
    except ProviderError as exc:
        log.error("order_payment_check_failed", order_id=str(order_id), error=exc.message)
        return False
    finally:
        client.close()

    return bool((response.data or {}).get("is_paid"))


def _publish_result(
    fulfillment: BillPaymentFulfillment, *, succeeded: bool, pending_review: bool = False
) -> None:
    OutboxEvent.objects.create(
        event_type="fulfillment.result",
        aggregate_type="BillPaymentFulfillment",
        aggregate_id=fulfillment.id,
        correlation_id=fulfillment.correlation_id,
        payload={
            "fulfillment_id": str(fulfillment.id),
            "order_id": str(fulfillment.order_id) if fulfillment.order_id else None,
            "succeeded": succeeded,
            "pending_review": pending_review,
            "provider_reference": fulfillment.provider_reference,
            "biller_reference": fulfillment.biller_reference,
            "failure_reason": fulfillment.failure_reason,
            "state": fulfillment.state,
        },
    )
