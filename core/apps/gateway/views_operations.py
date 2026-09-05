"""Cobro, comprobante e historial.

Estas vistas son comunes a recargas y a pago de servicios: una vez que existe
una orden, el cobro y el comprobante son iguales. Duplicarlos por servicio
haria que con el tiempo divergieran.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

import structlog
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.gateway.clients import billpay_client, payments_client, topups_client
from apps.tenancy.permissions import require_perm, user_has_perm
from samy_common.money import Money
from samy_common.providers.exceptions import ProviderError, ProviderNotConfigured

log = structlog.get_logger("bff.operations")


# ---------------------------------------------------------------------------
# Cobro
# ---------------------------------------------------------------------------

@login_required
@require_perm("operation.create")
@require_GET
def pay(request: HttpRequest, order_id: uuid.UUID) -> HttpResponse:
    """Pantalla de cobro. Muestra los metodos REALMENTE disponibles."""
    order = _get_order(request, order_id)

    # Solo se ofrecen metodos cuyo proveedor puede operar. Un boton de
    # "pagar con tarjeta" que devuelve 503 es un boton muerto.
    try:
        providers = payments_client().get("/api/v1/providers/").data or {}
        available = providers.get("providers", [])
    except ProviderError:
        available = []

    methods = _payment_methods(available)

    return render(
        request,
        "operations/pay.html",
        {
            "order": order,
            "methods": methods,
            "any_available": any(m["available"] for m in methods),
        },
    )


@login_required
@require_perm("operation.create")
@require_POST
def pay_cash(request: HttpRequest, order_id: uuid.UUID) -> HttpResponse:
    """Cobro en efectivo.

    Exige capturar cuanto entrego el cliente. El sistema calcula el cambio y
    rechaza montos insuficientes: marcar como pagada una orden por la que no
    se recibio el total produciria un faltante de caja inexplicable.
    """
    order = _get_order(request, order_id)
    raw = (request.POST.get("amount_tendered") or "").strip()

    try:
        tendered = Money.parse(Decimal(raw))
    except (InvalidOperation, ValueError, ArithmeticError):
        return render(
            request,
            "operations/_cash_form.html",
            {"order": order, "error": "Captura cuanto entrego el cliente."},
            status=422,
        )

    if tendered.cents < int(order["total_cents"]):
        return render(
            request,
            "operations/_cash_form.html",
            {
                "order": order,
                "error": (
                    f"Recibiste {tendered}, pero el total es "
                    f"{Money(int(order['total_cents']))}. Falta dinero."
                ),
                "amount_value": raw,
            },
            status=422,
        )

    idem = uuid.uuid4().hex
    try:
        # Se inicia el cobro en efectivo y se confirma en el mismo paso: el
        # cajero ya tiene el dinero en la mano.
        payments_client().post(
            f"/api/v1/orders/{order_id}/pay/",
            payload={
                "store_id": str(request.store.id),
                "actor_id": str(request.user.id),
                "method": "CASH",
            },
            idempotency_key=f"pay-{idem}",
        )
        confirmed = payments_client().post(
            f"/api/v1/orders/{order_id}/confirm-cash/",
            payload={
                "store_id": str(request.store.id),
                "actor_id": str(request.user.id),
                "amount_tendered_cents": tendered.cents,
            },
            idempotency_key=f"cash-{idem}",
        )
        result = confirmed.data or {}
    except ProviderNotConfigured as exc:
        return render(
            request,
            "topups/provider_unavailable.html",
            {"message": exc.message, "missing_requirements": list(exc.missing_requirements)},
            status=503,
        )
    except ProviderError as exc:
        log.error("cash_payment_failed", order_id=str(order_id), error=exc.message)
        return render(
            request,
            "operations/_cash_form.html",
            {"order": order, "error": exc.message, "amount_value": raw},
            status=422,
        )

    audit.record(
        request,
        AuditAction.PAYMENT_CONFIRMED,
        object_type="Order",
        object_id=str(order_id),
        previous_state=order.get("state", ""),
        new_state=result.get("state", ""),
        metadata={
            "method": "CASH",
            "folio": result.get("folio", ""),
            "tendered_cents": tendered.cents,
            "change_cents": result.get("change_cents", 0),
        },
    )

    request.session[f"change_{order_id}"] = result.get("change_cents", 0)
    return redirect("operations:receipt", order_id=order_id)


@login_required
@require_perm("operation.create")
@require_GET
def card_form(request: HttpRequest, order_id: uuid.UUID) -> HttpResponse:
    """Pantalla de captura de tarjeta.

    Lo unico que hace es cargar el tokenizador de Conekta. La tarjeta se
    escribe DENTRO de un iframe suyo: ni el numero ni el CVV pasan por este
    servidor, ni por esta plantilla, ni por nuestros logs. Lo que vuelve al
    servidor es un token de un solo uso.

    Se necesita la llave PUBLICA para inicializar el iframe. Es publica por
    diseno: solo sirve para tokenizar, no para cobrar.
    """
    order = _get_order(request, order_id)

    try:
        proveedores = payments_client().get("/api/v1/providers/").data or {}
        conekta = next(
            (
                p
                for p in proveedores.get("providers", [])
                if p.get("slug") == "conekta"
            ),
            None,
        )
    except ProviderError:
        conekta = None

    if not conekta or conekta.get("status") != "READY":
        return render(
            request,
            "topups/provider_unavailable.html",
            {
                "message": (conekta or {}).get(
                    "detail", "El cobro con tarjeta no esta disponible."
                ),
                "missing_requirements": (conekta or {}).get("missing_requirements", []),
            },
            status=503,
        )

    return render(
        request,
        "operations/card.html",
        {
            "order": order,
            "public_key": conekta.get("public_key", ""),
            "provider_mode": conekta.get("mode", ""),
        },
    )


@login_required
@require_perm("operation.create")
@require_POST
def pay_card(request: HttpRequest, order_id: uuid.UUID) -> HttpResponse:
    """Cobra con el token que genero el tokenizador en el navegador.

    Si el proveedor no tiene credenciales, se responde 503 con el detalle y
    **la orden no cambia de estado**. No hay simulacion posible.
    """
    order = _get_order(request, order_id)

    token = (request.POST.get("card_token") or "").strip()
    if not token:
        messages.error(
            request, "No se recibio el token de la tarjeta. Intenta de nuevo."
        )
        return redirect("operations:card", order_id=order_id)

    try:
        response = payments_client().post(
            f"/api/v1/orders/{order_id}/pay/",
            payload={
                "store_id": str(request.store.id),
                "actor_id": str(request.user.id),
                "method": "CARD",
                "card_token": token,
            },
            # La clave se deriva de la orden y del token, no de un aleatorio:
            # si el cajero reenvia el mismo formulario, la peticion es la
            # misma y el servicio la reconoce en vez de cobrar dos veces.
            idempotency_key=f"card-{order_id}-{token[-12:]}",
        )
        data = response.data or {}
    except ProviderNotConfigured as exc:
        return render(
            request,
            "topups/provider_unavailable.html",
            {"message": exc.message, "missing_requirements": list(exc.missing_requirements)},
            status=503,
        )
    except ProviderError as exc:
        # El token es de un solo uso: si el cobro fallo, hay que capturar la
        # tarjeta otra vez. Devolver al formulario es lo unico que funciona.
        log.warning(
            "card_payment_failed", order_id=str(order_id), error=exc.message
        )
        messages.error(request, exc.message)
        return redirect("operations:card", order_id=order_id)

    audit.record(
        request,
        AuditAction.PAYMENT_ATTEMPTED,
        object_type="Order",
        object_id=str(order_id),
        previous_state=order.get("state", ""),
        new_state=data.get("state", ""),
        metadata={"method": "CARD", "folio": data.get("folio", "")},
    )

    # Se va SIEMPRE al comprobante: es la pantalla que muestra el estado real
    # del pago y de la recarga, y se refresca sola mientras el desenlace se
    # decide. Anunciar exito aqui seria adelantarse al proveedor.
    return redirect("operations:receipt", order_id=order_id)


# ---------------------------------------------------------------------------
# Comprobante
# ---------------------------------------------------------------------------

@login_required
@require_perm("operation.view_own")
@require_GET
def receipt(request: HttpRequest, order_id: uuid.UUID) -> HttpResponse:
    """Comprobante imprimible.

    Muestra el estado REAL de la operacion. Si la recarga aun no se confirma,
    lo dice; no se anuncia un exito que todavia no ocurrio.
    """
    order = _get_order(request, order_id)
    fulfillment = _get_fulfillment(order)

    change_cents = request.session.pop(f"change_{order_id}", None)

    return render(
        request,
        "operations/receipt.html",
        {
            "order": order,
            "fulfillment": fulfillment,
            "store": request.store,
            "change_cents": change_cents,
            "change_display": _money(change_cents) if change_cents else "",
        },
    )


@login_required
@require_perm("operation.view_own")
@require_GET
def order_status(request: HttpRequest, order_id: uuid.UUID) -> HttpResponse:
    """Fragmento con el estado actual. HTMX lo consulta cada pocos segundos
    mientras la recarga se ejecuta, para que el cajero vea el desenlace sin
    recargar la pagina."""
    order = _get_order(request, order_id)
    fulfillment = _get_fulfillment(order)
    return render(
        request,
        "operations/_status.html",
        {"order": order, "fulfillment": fulfillment},
    )


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------

@login_required
@require_perm("operation.view_own")
@require_GET
def history(request: HttpRequest) -> HttpResponse:
    """Historial de operaciones.

    Un cajero ve las suyas; un dueño ve las de toda la tienda. El alcance lo
    decide el permiso, no un parametro que el cliente pueda cambiar.
    """
    scope = "store" if user_has_perm(request, "operation.view_store") else "own"

    params = {
        "store_id": str(request.store.id),
        "scope": scope,
        "limit": 50,
    }
    if scope == "own":
        params["user_id"] = str(request.user.id)
    if state := request.GET.get("state"):
        params["state"] = state

    try:
        response = payments_client().get("/api/v1/orders/history/", params=params)
        data = response.data or {}
        orders = data.get("results", [])
        unavailable = False
    except ProviderError as exc:
        log.warning("history_unavailable", error=exc.message)
        orders = []
        unavailable = True

    return render(
        request,
        "operations/history.html",
        {
            "orders": orders,
            "scope": scope,
            "unavailable": unavailable,
            "current_state": request.GET.get("state", ""),
        },
    )


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------

def _get_order(request: HttpRequest, order_id: uuid.UUID) -> dict:
    """Obtiene una orden, SIEMPRE acotada a la tienda activa.

    Se envia ``store_id`` para que el servicio de Pagos filtre. Si la orden es
    de otra tienda responde 404 y aqui se propaga como 404: no confirmamos ni
    siquiera que el identificador exista.
    """
    try:
        response = payments_client().get(
            f"/api/v1/orders/{order_id}/",
            params={"store_id": str(request.store.id)},
        )
    except ProviderError as exc:
        log.warning("order_fetch_failed", order_id=str(order_id), error=exc.message)
        raise Http404("Operacion no encontrada.") from exc

    if not response.data:
        raise Http404("Operacion no encontrada.")
    return _parse_datetimes(response.data)


#: Campos que llegan de la API como cadena ISO y las plantillas tratan como fecha.
_DATETIME_FIELDS = ("created_at", "paid_at", "completed_at", "expires_at", "sent_at")


def _parse_datetimes(data: dict) -> dict:
    """Convierte las fechas ISO de la API en objetos ``datetime``.

    El filtro ``|date`` de Django no formatea cadenas: si recibe texto lo
    devuelve vacio, y el comprobante saldria sin fecha. Como los servicios
    hablan JSON, la conversion tiene que hacerse aqui, en la frontera.
    """
    from django.utils.dateparse import parse_datetime

    for field in _DATETIME_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value:
            parsed = parse_datetime(value)
            if parsed is not None:
                data[field] = parsed
    return data


def _get_fulfillment(order: dict) -> dict | None:
    """Trae el detalle del servicio entregado (recarga o pago de recibo)."""
    fulfillment_id = order.get("fulfillment_id")
    if not fulfillment_id:
        return None

    client = topups_client() if order.get("service_kind") == "TOPUP" else billpay_client()
    path = (
        f"/api/v1/topups/{fulfillment_id}/"
        if order.get("service_kind") == "TOPUP"
        else f"/api/v1/payments/{fulfillment_id}/"
    )
    try:
        data = client.get(path, params={"store_id": order.get("store_id", "")}).data
    except ProviderError:
        return None
    return _parse_datetimes(data) if isinstance(data, dict) else data


def _payment_methods(providers: list[dict]) -> list[dict]:
    """Metodos de cobro con su disponibilidad REAL."""
    by_capability: dict[str, dict] = {}
    for provider in providers:
        for capability in provider.get("capabilities", []):
            by_capability.setdefault(capability, provider)

    def entry(key: str, label: str, description: str, capability: str, icon: str) -> dict:
        provider = by_capability.get(capability)
        ready = bool(provider and provider.get("status") == "READY")
        return {
            "key": key,
            "label": label,
            "description": description,
            "icon": icon,
            "available": ready,
            "reason": (provider or {}).get("detail", "No hay proveedor configurado."),
            "missing_requirements": (provider or {}).get("missing_requirements", []),
            "provider": (provider or {}).get("display_name", ""),
        }

    return [
        entry("cash", "Efectivo", "El cliente paga en el mostrador", "CASH_PAYMENT", "cash"),
        entry("card", "Tarjeta", "Debito o credito", "CARD_PAYMENT", "card"),
    ]


def _money(cents) -> str:
    return str(Money(int(cents or 0)))
