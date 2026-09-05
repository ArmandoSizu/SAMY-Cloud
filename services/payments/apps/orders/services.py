"""Casos de uso de ordenes: donde se hace cumplir la regla del dinero.

Este modulo es el corazon del sistema. Cada funcion representa un paso del
ciclo de vida de una operacion y ninguna permite saltarse el orden correcto.

    crear_orden          CREATED
    iniciar_cobro        CREATED -> PAYMENT_PENDING
    confirmar_pago       PAYMENT_PENDING -> PAID     ← unica puerta al servicio
    marcar_en_proceso    PAID -> PROCESSING
    registrar_resultado  PROCESSING -> SUCCESS | FAILED
    expirar / cancelar / reembolsar

La logica vive aqui y no en las vistas para que sea invocable desde HTTP, desde
un webhook, desde una tarea de Celery y desde una prueba, con exactamente el
mismo comportamiento. Una regla de dinero implementada dentro de una vista es
una regla que solo se cumple cuando el trafico llega por esa vista.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.commissions import engine as commissions
from apps.orders.models import (
    CommissionEntry,
    Order,
    OrderEvent,
    PaymentMethod,
    ServiceKind,
)
from apps.outbox.models import OutboxEvent
from apps.payments.models import PaymentAttempt, PaymentAttemptStatus
from apps.providers.base import PaymentIntent, PaymentOutcome
from apps.providers.registry import get_provider_for_method
from samy_common.money import Money
from samy_common.providers.exceptions import (
    ProviderIndeterminateError,
    ProviderPermanentError,
)
from samy_common.states import OrderState

log = structlog.get_logger("orders.services")


# ---------------------------------------------------------------------------
# Folio
# ---------------------------------------------------------------------------

def generate_folio(store_code: str) -> str:
    """Folio corto, legible y dictable por telefono.

    Formato: ``AAAA-YYMMDD-XXXX`` (codigo de tienda, fecha, aleatorio).

    Se usa un sufijo aleatorio y no un contador porque un contador secuencial
    revela el volumen de operaciones de la tienda y, peor, obliga a un bloqueo
    global que serializa todas las ventas.
    """
    import secrets

    alphabet = "ACDEFGHJKLMNPQRTUVWXY3479"  # sin caracteres ambiguos (0/O, 1/I)
    suffix = "".join(secrets.choice(alphabet) for _ in range(4))
    today = timezone.localtime().strftime("%y%m%d")
    return f"{store_code[:6].upper()}-{today}-{suffix}"


# ---------------------------------------------------------------------------
# Creacion
# ---------------------------------------------------------------------------

@transaction.atomic
def create_order(
    *,
    organization_id: uuid.UUID | str,
    store_id: uuid.UUID | str,
    store_code: str,
    created_by_id: uuid.UUID | str,
    created_by_email: str,
    service_kind: str,
    description: str,
    base_amount: Money,
    product_code: str = "",
    fulfillment_id: uuid.UUID | str | None = None,
    idempotency_key: str = "",
    correlation_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> Order:
    """Crea una orden con su comision ya calculada y congelada.

    Idempotente: si ya existe una orden con la misma clave en la misma tienda,
    se devuelve esa en vez de crear otra. Es lo que evita que el cajero, al
    pulsar dos veces por nerviosismo o mala señal, genere dos ventas.
    """
    if idempotency_key:
        existing = Order.objects.filter(
            store_id=store_id, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            log.info(
                "order_idempotent_hit",
                order_id=str(existing.id),
                folio=existing.folio,
                idempotency_key=idempotency_key,
            )
            return existing

    result = commissions.calculate(
        base=base_amount,
        service_kind=service_kind,
        store_id=store_id,
        organization_id=organization_id,
        product_code=product_code,
    )

    order = Order.objects.create(
        folio=generate_folio(store_code),
        organization_id=organization_id,
        store_id=store_id,
        created_by_id=created_by_id,
        created_by_email=created_by_email,
        service_kind=service_kind,
        fulfillment_id=fulfillment_id,
        description=description[:200],
        product_code=product_code,
        currency=base_amount.currency,
        base_cents=result.base.cents,
        commission_cents=result.commission.cents,
        total_cents=result.total.cents,
        state=OrderState.CREATED,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        metadata=metadata or {},
    )

    CommissionEntry.objects.create(
        order=order,
        currency=result.base.currency,
        commission_cents=result.commission.cents,
        store_share_cents=result.store_share.cents,
        platform_share_cents=result.platform_share.cents,
        provider_share_cents=result.provider_share.cents,
        rule_id=result.rule_id,
        rule_name=result.rule_name,
        rule_description=result.rule_description,
    )

    OrderEvent.objects.create(
        order=order,
        previous_state="",
        new_state=OrderState.CREATED.value,
        reason="Orden creada",
        actor_id=created_by_id,
        correlation_id=correlation_id,
    )

    log.info(
        "order_created",
        order_id=str(order.id),
        folio=order.folio,
        service_kind=service_kind,
        total_cents=order.total_cents,
    )
    return order


# ---------------------------------------------------------------------------
# Cobro
# ---------------------------------------------------------------------------

@transaction.atomic
def start_payment(
    *,
    order: Order,
    method: str,
    actor_id: uuid.UUID | str,
    card_token: str = "",
) -> tuple[Order, PaymentAttempt]:
    """Inicia el cobro contra el proveedor. ``CREATED -> PAYMENT_PENDING``.

    Si el proveedor no esta configurado, ``ensure_ready()`` levanta antes de
    tocar la orden: la orden se queda en ``CREATED`` y el cajero ve un mensaje
    claro. No se crea un intento fantasma ni se avanza el estado.

    ``card_token`` es el token que genero el tokenizador del proveedor en el
    navegador. Nunca es un numero de tarjeta: el PAN no pasa por aqui.

    **Guarda contra doble cobro.** Conekta no ofrece cabecera de idempotencia,
    asi que la proteccion la ponemos nosotros: si la orden ya tiene un intento
    vivo con referencia del proveedor, no se crea otro. Sin esto, dos clics en
    "Pagar" serian dos cargos a la misma tarjeta.
    """
    provider = get_provider_for_method(method)
    provider.ensure_ready()

    vivo = (
        order.attempts.filter(
            status__in=[
                PaymentAttemptStatus.INITIATED,
                PaymentAttemptStatus.AWAITING_CUSTOMER,
            ]
        )
        .exclude(provider_reference="")
        .order_by("-created_at")
        .first()
    )
    if vivo is not None:
        log.info(
            "payment_attempt_reused",
            order_id=str(order.id),
            folio=order.folio,
            attempt_id=str(vivo.id),
            provider_reference=vivo.provider_reference,
        )
        return order, vivo

    ttl = _qr_ttl_seconds()
    expires_at = timezone.now() + timedelta(seconds=ttl)

    attempt = PaymentAttempt.objects.create(
        order=order,
        provider_slug=provider.slug,
        provider_mode=str(provider.mode),
        method=method,
        amount_cents=order.total_cents,
        currency=order.currency,
        status=PaymentAttemptStatus.INITIATED,
        # La clave de idempotencia enviada al proveedor incluye el id del
        # intento: un reintento del MISMO intento no duplica el cargo, pero un
        # intento nuevo (tras uno fallido) si puede cobrar.
        idempotency_key=f"{order.id}:{uuid.uuid4().hex[:12]}",
        correlation_id=order.correlation_id,
        expires_at=expires_at,
    )

    intent = PaymentIntent(
        order_id=order.id,
        folio=order.folio,
        amount=order.total,
        description=order.description,
        idempotency_key=attempt.idempotency_key,
        store_id=order.store_id,
        method=method,
        # El token viaja hasta el adaptador y muere ahi. No se guarda en la
        # base ni se escribe en ningun log: es de un solo uso y, aunque no sea
        # un PAN, no hay motivo para conservarlo.
        card_token=card_token,
        expires_at=expires_at,
        metadata={"service_kind": order.service_kind},
    )

    try:
        result = provider.create_payment(intent)
    except ProviderIndeterminateError:
        # No sabemos si el cargo se creo. La orden va a revision y el intento
        # queda marcado: una tarea de conciliacion consultara el estado real.
        attempt.mark_indeterminate("Timeout al crear el cobro.")
        order.transition(
            OrderState.PAYMENT_PENDING,
            reason="Cobro enviado, respuesta desconocida.",
            actor_id=actor_id,
        )
        order.transition(
            OrderState.UNDER_REVIEW,
            reason="Respuesta indeterminada del proveedor de pago.",
            actor_id=actor_id,
        )
        raise
    except ProviderPermanentError as exc:
        attempt.mark_failed(exc.message)
        raise

    attempt.apply_provider_result(result)

    order.provider_mode = result.provider_mode
    order.payment_method = method
    order.expires_at = expires_at
    order.save(update_fields=["provider_mode", "payment_method", "expires_at", "updated_at"])

    order.transition(
        OrderState.PAYMENT_PENDING,
        reason=f"Cobro iniciado con {provider.display_name}.",
        actor_id=actor_id,
        metadata={"attempt_id": str(attempt.id), "provider": provider.slug},
    )

    # Si el proveedor confirmo de inmediato (poco comun, pero posible en
    # algunos flujos), se avanza en el acto.
    if result.outcome == PaymentOutcome.CONFIRMED:
        confirm_payment(
            order=order,
            attempt=attempt,
            provider_reference=result.provider_reference,
            source="respuesta_sincrona",
            actor_id=actor_id,
        )

    return order, attempt


@transaction.atomic
def confirm_payment(
    *,
    order: Order,
    attempt: PaymentAttempt | None,
    provider_reference: str,
    source: str,
    actor_id: uuid.UUID | str | None = None,
    amount_received: Money | None = None,
) -> Order:
    """Confirma el pago. ``PAYMENT_PENDING -> PAID``.

    **Esta es la unica puerta hacia la ejecucion del servicio.** Solo se llama
    desde:

    * un webhook con firma verificada,
    * una consulta de estado al proveedor que devolvio CONFIRMED,
    * la confirmacion de efectivo por parte del cajero autenticado.

    Es idempotente: un webhook reenviado encuentra la orden ya en ``PAID`` y
    no vuelve a emitir el evento que dispara la recarga.
    """
    fresh = Order.objects.select_for_update().get(pk=order.pk)

    if fresh.state_enum in {OrderState.PAID, OrderState.PROCESSING, OrderState.SUCCESS}:
        log.info(
            "payment_confirmation_ignored_already_paid",
            order_id=str(fresh.id),
            folio=fresh.folio,
            state=fresh.state,
            source=source,
        )
        return fresh

    if amount_received is not None and amount_received < fresh.total:
        raise ProviderPermanentError(
            provider=source,
            message=(
                f"El monto confirmado ({amount_received}) es menor al total de "
                f"la orden ({fresh.total}). No se marca como pagada."
            ),
        )

    if attempt is not None:
        attempt.mark_confirmed(provider_reference)

    fresh.transition(
        OrderState.PAID,
        reason=f"Pago confirmado ({source}).",
        actor_id=actor_id,
        metadata={"provider_reference": provider_reference, "source": source},
    )

    # Outbox: el evento y el cambio de estado se guardan en la MISMA
    # transaccion. Si el proceso muere aqui, o se guardaron los dos o ninguno.
    # Nunca puede ocurrir "el cliente pago y nadie se entero".
    OutboxEvent.objects.create(
        event_type="order.paid",
        aggregate_type="Order",
        aggregate_id=fresh.id,
        correlation_id=fresh.correlation_id,
        payload={
            "order_id": str(fresh.id),
            "folio": fresh.folio,
            "store_id": str(fresh.store_id),
            "service_kind": fresh.service_kind,
            "fulfillment_id": str(fresh.fulfillment_id) if fresh.fulfillment_id else None,
            "base_cents": fresh.base_cents,
            "total_cents": fresh.total_cents,
            "currency": fresh.currency,
            "provider_reference": provider_reference,
            "provider_mode": fresh.provider_mode,
            "paid_at": fresh.paid_at.isoformat() if fresh.paid_at else None,
        },
    )

    log.info(
        "order_paid",
        order_id=str(fresh.id),
        folio=fresh.folio,
        total_cents=fresh.total_cents,
        source=source,
    )

    # Una venta propia del comercio no tiene servicio externo que entregar:
    # el cliente ya se llevo lo que compro. Dejarla en PAID esperando una
    # confirmacion que nunca llegara la mostraria "en proceso" para siempre.
    if fresh.service_kind == ServiceKind.MERCHANT_SALE:
        fresh = fresh.transition(
            OrderState.PROCESSING, reason="Venta del comercio: sin entrega externa."
        )
        fresh = fresh.transition(
            OrderState.SUCCESS, reason="Venta completada."
        )
        OutboxEvent.objects.create(
            event_type="order.succeeded",
            aggregate_type="Order",
            aggregate_id=fresh.id,
            correlation_id=fresh.correlation_id,
            payload={"order_id": str(fresh.id), "folio": fresh.folio},
        )

    return fresh


# ---------------------------------------------------------------------------
# Ejecucion del servicio
# ---------------------------------------------------------------------------

@transaction.atomic
def mark_processing(*, order: Order, reason: str = "") -> Order:
    """``PAID -> PROCESSING``. Lo llama el microservicio que ejecuta."""
    return order.transition(
        OrderState.PROCESSING, reason=reason or "Servicio en ejecucion."
    )


@transaction.atomic
def record_fulfillment_result(
    *,
    order: Order,
    succeeded: bool,
    provider_reference: str = "",
    reason: str = "",
) -> Order:
    """Cierra la operacion segun lo que reporto el microservicio ejecutor.

    Si el servicio fallo pero el dinero ya se cobro, la orden pasa
    automaticamente a ``REFUND_PENDING``. No queda a criterio de nadie: el
    cliente pago por algo que no recibio y la devolucion se dispara sola.
    """
    if succeeded:
        updated = order.transition(
            OrderState.SUCCESS,
            reason=reason or "Servicio entregado.",
            metadata={"provider_reference": provider_reference},
        )
        OutboxEvent.objects.create(
            event_type="order.succeeded",
            aggregate_type="Order",
            aggregate_id=updated.id,
            correlation_id=updated.correlation_id,
            payload={"order_id": str(updated.id), "folio": updated.folio},
        )
        return updated

    updated = order.transition(
        OrderState.FAILED,
        reason=reason or "El proveedor no pudo entregar el servicio.",
        metadata={"provider_reference": provider_reference},
    )
    updated = updated.transition(
        OrderState.REFUND_PENDING,
        reason="Se cobro pero el servicio no se entrego. Reembolso automatico.",
    )
    OutboxEvent.objects.create(
        event_type="order.refund_required",
        aggregate_type="Order",
        aggregate_id=updated.id,
        correlation_id=updated.correlation_id,
        payload={
            "order_id": str(updated.id),
            "folio": updated.folio,
            "amount_cents": updated.total_cents,
            "reason": reason,
        },
    )
    log.warning(
        "order_failed_refund_required",
        order_id=str(updated.id),
        folio=updated.folio,
        reason=reason,
    )
    return updated


# ---------------------------------------------------------------------------
# Cancelacion y expiracion
# ---------------------------------------------------------------------------

@transaction.atomic
def cancel_order(*, order: Order, reason: str, actor_id: uuid.UUID | str) -> Order:
    """Cancela una orden que aun no se ha cobrado."""
    return order.transition(OrderState.CANCELLED, reason=reason, actor_id=actor_id)


@transaction.atomic
def expire_order(*, order: Order) -> Order:
    """Expira un intento de cobro vencido.

    Antes de expirar se consulta el estado real al proveedor: si el cliente
    pago en el ultimo segundo y el webhook se retraso, expirar sin preguntar
    dejaria un cobro sin servicio. Preguntar cuesta una llamada; equivocarse
    cuesta el dinero de un cliente.
    """
    attempt = order.attempts.order_by("-created_at").first()
    if attempt and attempt.provider_reference and attempt.provider_slug != "cash":
        from apps.providers.registry import get_provider

        try:
            provider = get_provider(attempt.provider_slug)
            status = provider.get_payment_status(attempt.provider_reference)
            if status.outcome == PaymentOutcome.CONFIRMED:
                log.warning(
                    "order_expiry_aborted_payment_found",
                    order_id=str(order.id),
                    folio=order.folio,
                )
                return confirm_payment(
                    order=order,
                    attempt=attempt,
                    provider_reference=status.provider_reference,
                    source="conciliacion_previa_a_expiracion",
                )
        except Exception as exc:  # noqa: BLE001
            # Si no se puede consultar, la orden va a revision en vez de
            # expirar: es preferible una orden pendiente de revisar a un
            # cliente que pago y se quedo sin nada.
            log.error(
                "order_expiry_status_check_failed",
                order_id=str(order.id),
                error=str(exc),
            )
            return order.transition(
                OrderState.UNDER_REVIEW,
                reason="Vencio el cobro y no se pudo verificar el estado real.",
            )

    return order.transition(
        OrderState.EXPIRED, reason="El tiempo para completar el pago expiro."
    )


def _qr_ttl_seconds() -> int:
    """Vigencia del intento de cobro, acotada al rango permitido."""
    ttl = int(getattr(settings, "QR_TOKEN_TTL_SECONDS", 240))
    return max(settings.QR_TOKEN_TTL_MIN, min(ttl, settings.QR_TOKEN_TTL_MAX))
