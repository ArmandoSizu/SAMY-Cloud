"""Ejecucion de recargas.

Aqui vive la mitad de la regla del dinero que le toca a este servicio:

    ``execute_topup()`` REHUSA ejecutar si la orden no esta pagada.

No confia en que el evento ``order.paid`` sea autentico: **vuelve a preguntar
al servicio de Pagos** por el estado real de la orden antes de gastar saldo del
proveedor. Un evento puede llegar duplicado, retrasado o (en el peor caso)
falsificado; una consulta firmada al dueno del dato, no.

Es una llamada HTTP adicional por recarga. Vale la pena: el costo de recargar
un telefono sin haber cobrado es exactamente el monto de la recarga, cada vez.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import TopupProduct
from apps.fulfillment.models import TopupFulfillment
from apps.outbox.models import OutboxEvent
from apps.providers.base import TopupRequest, TopupStatus
from apps.providers.registry import get_provider
from samy_common.http.client import ServiceClient, ServiceClientConfig
from samy_common.money import Money
from samy_common.phone import normalize_mx_phone
from samy_common.providers.exceptions import (
    ProviderError,
    ProviderIndeterminateError,
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.security.masking import mask_phone
from samy_common.states import FulfillmentState

log = structlog.get_logger("fulfillment.services")


@transaction.atomic
def create_fulfillment(
    *,
    organization_id: uuid.UUID | str,
    store_id: uuid.UUID | str,
    requested_by_id: uuid.UUID | str,
    product_id: uuid.UUID | str,
    phone_raw: str,
    amount: Money | None = None,
    correlation_id: str = "",
) -> TopupFulfillment:
    """Registra la intencion de recargar. Aun NO recarga nada.

    Valida en el servidor: el numero telefonico, que el producto siga activo y
    que el monto sea uno que el proveedor efectivamente vende. La UI ya filtra,
    pero cualquiera puede saltarse la UI y llamar a la API directamente.
    """
    product = (
        TopupProduct.objects.select_related("operator")
        .filter(pk=product_id, is_active=True)
        .first()
    )
    if product is None:
        raise ValidationError(
            "Ese producto ya no esta disponible. Actualiza el catalogo."
        )

    # Validacion del numero. Levanta con un mensaje que el cajero entiende.
    phone = normalize_mx_phone(phone_raw)

    resolved_amount = amount or product.amount
    if resolved_amount is None:
        raise ValidationError(
            "Este producto requiere que captures un monto."
        )
    product.validate_amount(resolved_amount)

    fulfillment = TopupFulfillment.objects.create(
        organization_id=organization_id,
        store_id=store_id,
        requested_by_id=requested_by_id,
        product=product,
        operator_name=product.operator.name,
        product_label=product.label,
        phone_e164=phone.e164,
        phone_masked=mask_phone(phone.national),
        currency=resolved_amount.currency,
        amount_cents=resolved_amount.cents,
        state=FulfillmentState.PENDING_PAYMENT,
        provider_slug=product.provider_slug,
        # Clave estable por recarga: el proveedor la usa para no duplicar.
        idempotency_key=f"topup:{uuid.uuid4().hex}",
        correlation_id=correlation_id,
    )

    log.info(
        "fulfillment_created",
        fulfillment_id=str(fulfillment.id),
        operator=product.operator.name,
        phone_masked=fulfillment.phone_masked,
        amount_cents=resolved_amount.cents,
    )
    return fulfillment


def execute_topup(*, fulfillment: TopupFulfillment) -> TopupFulfillment:
    """Ejecuta la recarga contra el proveedor.

    Precondicion NO NEGOCIABLE: la orden asociada debe estar pagada, y eso se
    verifica preguntandole al servicio de Pagos, no confiando en el evento que
    disparo esta ejecucion.
    """
    if fulfillment.state_enum not in {
        FulfillmentState.QUEUED,
        FulfillmentState.PENDING_PAYMENT,
    }:
        log.info(
            "topup_execution_skipped",
            fulfillment_id=str(fulfillment.id),
            state=fulfillment.state,
        )
        return fulfillment

    if not fulfillment.order_id:
        raise ProviderPermanentError(
            provider="topups",
            message="La recarga no tiene orden asociada; no se puede ejecutar.",
        )

    # --- Verificacion independiente del pago -----------------------------
    if not _order_is_paid(fulfillment.order_id):
        log.error(
            "topup_execution_blocked_order_not_paid",
            fulfillment_id=str(fulfillment.id),
            order_id=str(fulfillment.order_id),
        )
        raise ProviderPermanentError(
            provider="topups",
            message=(
                "La orden asociada no esta pagada. La recarga no se ejecuta."
            ),
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
        FulfillmentState.SENT, reason=f"Enviada a {provider.display_name}."
    )

    request = TopupRequest(
        fulfillment_id=fulfillment.id,
        order_id=fulfillment.order_id,
        operator_code=fulfillment.product.operator.provider_operator_id,
        product_id=fulfillment.product.provider_product_id,
        amount=fulfillment.amount,
        phone_e164=fulfillment.phone_e164,
        phone_masked=fulfillment.phone_masked,
        idempotency_key=fulfillment.idempotency_key,
    )

    try:
        result = provider.send_topup(request)
    except ProviderIndeterminateError as exc:
        # No sabemos si se recargo. NUNCA se reintenta a ciegas: se marca para
        # conciliacion, que consultara el estado real por la clave de
        # idempotencia. Reintentar aqui podria recargar dos veces.
        fulfillment.failure_reason = exc.message[:255]
        fulfillment.save(update_fields=["failure_reason", "updated_at"])
        fulfillment.transition(
            FulfillmentState.UNDER_REVIEW,
            reason="Respuesta indeterminada del proveedor.",
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
    fulfillment.operator_reference = result.operator_reference[:128]
    fulfillment.raw_response = result.raw_response
    fulfillment.save(
        update_fields=[
            "provider_reference",
            "operator_reference",
            "raw_response",
            "updated_at",
        ]
    )

    if result.status == TopupStatus.SUCCEEDED:
        fulfillment = fulfillment.transition(
            FulfillmentState.SUCCEEDED, reason="El operador confirmo la recarga."
        )
        _publish_result(fulfillment, succeeded=True)
    elif result.status == TopupStatus.FAILED:
        fulfillment.failure_reason = (result.failure_reason or "Rechazada por el operador.")[:255]
        fulfillment.save(update_fields=["failure_reason", "updated_at"])
        fulfillment = fulfillment.transition(
            FulfillmentState.FAILED, reason=fulfillment.failure_reason
        )
        _publish_result(fulfillment, succeeded=False)
    else:
        # PENDING o UNKNOWN: sigue en curso. Se deja en SENT y la tarea de
        # conciliacion consultara el resultado. No se cierra por optimismo.
        log.info(
            "topup_pending_at_provider",
            fulfillment_id=str(fulfillment.id),
            status=result.status,
        )

    return fulfillment


def _order_is_paid(order_id: uuid.UUID) -> bool:
    """Pregunta al servicio de Pagos si la orden esta realmente pagada.

    Ante cualquier duda (servicio caido, respuesta rara) devuelve False. El
    valor seguro es no recargar: una recarga no ejecutada se puede reintentar;
    una recarga regalada no se recupera.
    """
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
        log.error(
            "order_payment_check_failed",
            order_id=str(order_id),
            error=exc.message,
        )
        return False
    finally:
        client.close()

    data = response.data or {}
    return bool(data.get("is_paid"))


def _publish_result(
    fulfillment: TopupFulfillment, *, succeeded: bool, pending_review: bool = False
) -> None:
    """Avisa al servicio de Pagos como termino la entrega.

    Va por outbox transaccional: el evento se guarda con el cambio de estado,
    de modo que no exista "la recarga fallo pero nadie disparo el reembolso".
    """
    OutboxEvent.objects.create(
        event_type="fulfillment.result",
        aggregate_type="TopupFulfillment",
        aggregate_id=fulfillment.id,
        correlation_id=fulfillment.correlation_id,
        payload={
            "fulfillment_id": str(fulfillment.id),
            "order_id": str(fulfillment.order_id) if fulfillment.order_id else None,
            "succeeded": succeeded,
            "pending_review": pending_review,
            "provider_reference": fulfillment.provider_reference,
            "operator_reference": fulfillment.operator_reference,
            "failure_reason": fulfillment.failure_reason,
            "state": fulfillment.state,
        },
    )
