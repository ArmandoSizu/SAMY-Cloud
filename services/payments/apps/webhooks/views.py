"""Recepcion de webhooks de proveedores de pago.

Un webhook es **una instruccion de un desconocido para marcar dinero como
cobrado**. Todo lo que sigue existe por eso.

Orden estricto de operaciones:

1. Se verifica la firma criptografica ANTES de interpretar el cuerpo.
   Sin firma valida, se rechaza y se registra. Nunca "por si acaso".
2. Se deduplica por ``event_id``: los proveedores reenvian eventos cuando no
   reciben un 200 a tiempo, y procesar dos veces el mismo evento duplicaria
   la recarga que dispara.
3. Se localiza la orden por la referencia del proveedor.
4. Se confirma el pago, que a su vez es idempotente.
5. Se responde 200 rapido. Un webhook lento provoca reintentos del proveedor.

Estas rutas estan exentas de la firma S2S interna (traen la suya propia), pero
NO estan exentas de verificacion: la del proveedor es mas fuerte, porque usa
criptografia asimetrica.
"""

from __future__ import annotations

import structlog
from django.core.cache import cache
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.orders import services
from apps.orders.models import Order
from apps.payments.models import PaymentAttempt
from apps.providers.base import PaymentOutcome
from apps.providers.registry import get_provider
from samy_common.providers.exceptions import ProviderError

log = structlog.get_logger("webhooks")

DEDUPE_PREFIX = "samy:webhook:seen:"


@csrf_exempt
@require_POST
def conekta(request: HttpRequest) -> JsonResponse:
    """Webhook de Conekta. Firma RSA-SHA256 en la cabecera ``digest``."""
    return _handle(request, provider_slug="conekta")


def _handle(request: HttpRequest, *, provider_slug: str) -> JsonResponse:
    try:
        provider = get_provider(provider_slug)
    except ProviderError as exc:
        log.error("webhook_provider_unavailable", provider=provider_slug, error=exc.message)
        # 503 y no 400: el proveedor reintentara cuando lo configuremos.
        return JsonResponse({"error": "provider_unavailable"}, status=503)

    # --- 1. Verificacion criptografica -----------------------------------
    try:
        event = provider.verify_webhook(
            payload=request.body, headers=dict(request.headers)
        )
    except ProviderError as exc:
        log.error(
            "webhook_rejected",
            provider=provider_slug,
            reason=exc.message,
            remote_addr=request.META.get("REMOTE_ADDR"),
        )
        # 400 definitivo: que el proveedor no reintente un evento no valido.
        return JsonResponse({"error": "invalid_signature"}, status=400)

    # --- 2. Deduplicacion -------------------------------------------------
    if event.event_id:
        key = f"{DEDUPE_PREFIX}{provider_slug}:{event.event_id}"
        # ``cache.add`` es atomico: devuelve False si la clave ya existia.
        if not cache.add(key, "1", timeout=settings.WEBHOOK_DEDUPE_WINDOW_SECONDS):
            log.info(
                "webhook_duplicate_ignored",
                provider=provider_slug,
                event_id=event.event_id,
            )
            # 200: el evento ya se proceso. Devolver error haria que el
            # proveedor lo reintentara indefinidamente.
            return JsonResponse({"status": "duplicate_ignored"})

    log.info(
        "webhook_received",
        provider=provider_slug,
        event_id=event.event_id,
        event_type=event.event_type,
        outcome=event.outcome,
    )

    # --- 3. Localizar la orden -------------------------------------------
    attempt = (
        PaymentAttempt.objects.filter(
            provider_slug=provider_slug, provider_reference=event.provider_reference
        )
        .select_related("order")
        .order_by("-created_at")
        .first()
    )

    if attempt is None:
        # Puede ser un evento de otro entorno (sandbox contra produccion) o de
        # una orden ya purgada. Se responde 200 para que el proveedor deje de
        # reintentar, pero se registra como aviso para investigarlo.
        log.warning(
            "webhook_order_not_found",
            provider=provider_slug,
            provider_reference=event.provider_reference,
            event_id=event.event_id,
        )
        return JsonResponse({"status": "order_not_found"})

    order: Order = attempt.order

    # --- 4. Aplicar el resultado ------------------------------------------
    if event.outcome == PaymentOutcome.CONFIRMED:
        try:
            services.confirm_payment(
                order=order,
                attempt=attempt,
                provider_reference=event.provider_reference,
                source=f"webhook:{provider_slug}",
                amount_received=event.amount,
            )
        except ProviderError as exc:
            # El monto confirmado no coincide con el de la orden. NO se marca
            # como pagada: es mejor una orden en revision que una entrega
            # regalada por una diferencia de importe.
            log.error(
                "webhook_confirmation_rejected",
                order_id=str(order.id),
                folio=order.folio,
                reason=exc.message,
            )
            return JsonResponse({"status": "rejected", "reason": exc.message}, status=200)

    elif event.outcome == PaymentOutcome.DECLINED:
        attempt.mark_failed(f"Rechazado por {provider_slug}.")
        log.info("webhook_payment_declined", order_id=str(order.id), folio=order.folio)

    else:
        # PENDING o UNKNOWN: no se cambia nada. La conciliacion decidira con
        # una consulta directa al proveedor.
        log.info(
            "webhook_non_final_outcome",
            order_id=str(order.id),
            outcome=event.outcome,
        )

    return JsonResponse({"status": "ok"})
