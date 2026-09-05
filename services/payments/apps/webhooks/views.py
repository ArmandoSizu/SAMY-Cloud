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

import uuid

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.orders import services
from apps.orders.models import Order
from apps.payments.models import PaymentAttempt
from apps.providers.base import PaymentOutcome
from apps.providers.registry import get_provider
from apps.webhooks.models import ReceivedWebhook, WebhookProcessingResult
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

    # --- 2. Deduplicacion, en dos capas ----------------------------------
    #
    # Cache primero porque es barata y corta el 99% de los reenvios sin tocar
    # la base. Pero la cache es memoria: se pierde al reiniciar Redis. La
    # barrera que de verdad garantiza "una sola vez" es la restriccion unica
    # de la tabla, que ademas resiste dos peticiones simultaneas.
    if event.event_id:
        key = f"{DEDUPE_PREFIX}{provider_slug}:{event.event_id}"
        # ``cache.add`` es atomico: devuelve False si la clave ya existia.
        if not cache.add(key, "1", timeout=settings.WEBHOOK_DEDUPE_WINDOW_SECONDS):
            log.info(
                "webhook_duplicate_ignored",
                provider=provider_slug,
                event_id=event.event_id,
                capa="cache",
            )
            # 200: el evento ya se proceso. Devolver error haria que el
            # proveedor lo reintentara indefinidamente.
            return JsonResponse({"status": "duplicate_ignored"})

        try:
            with transaction.atomic():
                registro = ReceivedWebhook.objects.create(
                    provider_slug=provider_slug,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    provider_reference=event.provider_reference,
                    outcome=event.outcome,
                    amount_cents=event.amount.cents if event.amount else None,
                    payload=event.raw_payload,
                    correlation_id=getattr(request, "correlation_id", "") or "",
                )
        except IntegrityError:
            # La cache no lo tenia (se reinicio Redis) pero la base si. Este
            # es exactamente el caso que la cache sola no cubre.
            log.info(
                "webhook_duplicate_ignored",
                provider=provider_slug,
                event_id=event.event_id,
                capa="base_de_datos",
            )
            return JsonResponse({"status": "duplicate_ignored"})
    else:
        # Un evento sin identificador no se puede deduplicar. Se procesa, pero
        # queda anotado: si esto pasa seguido, hay que revisar el adaptador.
        log.warning("webhook_sin_event_id", provider=provider_slug)
        registro = ReceivedWebhook.objects.create(
            provider_slug=provider_slug,
            event_id=f"sin-id:{uuid.uuid4().hex}",
            event_type=event.event_type,
            provider_reference=event.provider_reference,
            outcome=event.outcome,
            amount_cents=event.amount.cents if event.amount else None,
            payload=event.raw_payload,
            correlation_id=getattr(request, "correlation_id", "") or "",
        )

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
        # reintentar, pero queda registrado para poder investigarlo.
        log.warning(
            "webhook_order_not_found",
            provider=provider_slug,
            provider_reference=event.provider_reference,
            event_id=event.event_id,
        )
        _anotar(registro, WebhookProcessingResult.ORDER_NOT_FOUND)
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
            _anotar(registro, WebhookProcessingResult.REJECTED, exc.message)
            return JsonResponse({"status": "rejected", "reason": exc.message}, status=200)

        _anotar(registro, WebhookProcessingResult.APPLIED, f"Orden {order.folio} pagada.")

    elif event.outcome == PaymentOutcome.DECLINED:
        attempt.mark_failed(f"Rechazado por {provider_slug}.")
        log.info("webhook_payment_declined", order_id=str(order.id), folio=order.folio)
        _anotar(registro, WebhookProcessingResult.APPLIED, "Cobro rechazado.")

    else:
        # PENDING o UNKNOWN: no se cambia nada. La conciliacion decidira con
        # una consulta directa al proveedor.
        log.info(
            "webhook_non_final_outcome",
            order_id=str(order.id),
            outcome=event.outcome,
        )
        _anotar(
            registro,
            WebhookProcessingResult.IGNORED,
            f"Estado no terminal: {event.outcome}.",
        )

    return JsonResponse({"status": "ok"})


def _anotar(
    registro: ReceivedWebhook, resultado: str, detalle: str = ""
) -> None:
    """Deja constancia de que se hizo con el evento.

    Nunca propaga: si la anotacion falla, el webhook ya surtio efecto y
    devolver un error haria que el proveedor reenviara un evento que ya se
    aplico. Perder una linea de bitacora es malo; provocar un reenvio de algo
    ya procesado es peor.
    """
    try:
        registro.result = resultado
        registro.detail = detalle[:300]
        registro.save(update_fields=["result", "detail"])
    except Exception as exc:  # noqa: BLE001
        log.error("webhook_anotacion_fallida", error=str(exc))
