"""Flujo de recargas visto desde el Core (Backend For Frontend).

El navegador nunca habla con el microservicio de recargas ni con el de pagos.
Estas vistas orquestan: reciben la accion del cajero, resuelven la autorizacion
multi-tenant una sola vez, y llaman a los servicios con peticiones firmadas.

El flujo son cinco pantallas encadenadas con HTMX. Cada paso valida en el
servidor: la UI filtra por comodidad, pero cualquiera puede saltarse la UI.

    catalogo -> numero -> producto -> confirmacion -> cobro -> comprobante
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

import structlog
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.gateway.clients import payments_client, topups_client
from apps.tenancy.permissions import require_perm
from samy_common.money import Money
from samy_common.phone import PhoneValidationError, normalize_mx_phone
from samy_common.providers.exceptions import ProviderError, ProviderNotConfigured

log = structlog.get_logger("bff.topups")


# ---------------------------------------------------------------------------
# 1. Catalogo de compañias
# ---------------------------------------------------------------------------

@login_required
@require_perm("operation.create")
@require_GET
def catalog(request: HttpRequest) -> HttpResponse:
    """Catalogo real de operadores.

    Si el proveedor no esta configurado, se muestra un estado vacio que
    explica exactamente que falta. NUNCA se inventan operadores ni montos.
    """
    try:
        response = topups_client().get("/api/v1/catalog/")
        data = response.data or {}
    except ProviderError as exc:
        log.warning("catalog_unavailable", error=exc.message, code=exc.code)
        data = {
            "operators": [],
            "available": False,
            "reason": (
                "El servicio de recargas no responde. "
                "Intenta de nuevo en unos segundos."
            ),
            "provider_status": "UNREACHABLE",
            "missing_requirements": [],
        }

    return render(
        request,
        "topups/catalog.html",
        {
            "operators": data.get("operators", []),
            "available": data.get("available", False),
            "reason": data.get("reason", ""),
            "provider_status": data.get("provider_status", ""),
            "missing_requirements": data.get("missing_requirements", []),
            "is_stale": data.get("is_stale", False),
            "provider_mode": data.get("provider_mode", ""),
        },
    )


# ---------------------------------------------------------------------------
# 2. Captura del numero
# ---------------------------------------------------------------------------

@login_required
@require_perm("operation.create")
@require_GET
def phone_step(request: HttpRequest, operator_id: uuid.UUID) -> HttpResponse:
    """Pantalla de captura del numero para un operador."""
    operator = _find_operator(operator_id)
    if operator is None:
        messages.error(request, "Esa compañia ya no esta disponible.")
        return redirect("topups:catalog")

    return render(request, "topups/phone.html", {"operator": operator})


@login_required
@require_perm("operation.create")
@require_POST
def validate_phone(request: HttpRequest, operator_id: uuid.UUID) -> HttpResponse:
    """Valida el numero en el SERVIDOR y avanza a la seleccion de producto.

    La validacion del navegador es una ayuda de usabilidad; esta es la que
    cuenta. Un numero mal capturado significa recargar el telefono de otra
    persona, y ese dinero no se recupera.
    """
    operator = _find_operator(operator_id)
    if operator is None:
        messages.error(request, "Esa compañia ya no esta disponible.")
        return redirect("topups:catalog")

    raw = (request.POST.get("phone") or "").strip()
    confirm = (request.POST.get("phone_confirm") or "").strip()

    try:
        phone = normalize_mx_phone(raw)
    except PhoneValidationError as exc:
        return render(
            request,
            "topups/_phone_form.html",
            {"operator": operator, "error": str(exc), "phone_value": raw},
            status=422,
        )

    # Doble captura: el cajero escribe el numero dos veces. Es la practica
    # estandar en punto de venta y atrapa el error mas caro del flujo.
    if confirm:
        try:
            phone_confirm = normalize_mx_phone(confirm)
        except PhoneValidationError:
            phone_confirm = None
        if phone_confirm is None or phone_confirm.national != phone.national:
            return render(
                request,
                "topups/_phone_form.html",
                {
                    "operator": operator,
                    "error": "Los dos numeros no coinciden. Verificalos.",
                    "phone_value": raw,
                },
                status=422,
            )

    request.session["topup_draft"] = {
        "operator_id": str(operator_id),
        "operator_name": operator.get("name", ""),
        "phone": phone.national,
    }

    return render(
        request,
        "topups/_products.html",
        {"operator": operator, "phone": phone},
    )


# ---------------------------------------------------------------------------
# 3. Confirmacion
# ---------------------------------------------------------------------------

@login_required
@require_perm("operation.create")
@require_POST
def confirm_step(request: HttpRequest) -> HttpResponse:
    """Muestra el desglose antes de cobrar: producto, comision y total.

    El calculo de la comision lo hace el servicio de Pagos, no el navegador.
    Aqui solo se muestra lo que ese servicio respondio.
    """
    draft = request.session.get("topup_draft") or {}
    product_id = request.POST.get("product_id", "")
    amount_raw = (request.POST.get("amount") or "").strip()

    if not draft.get("phone") or not product_id:
        messages.error(request, "La operacion expiro. Vuelve a empezar.")
        return redirect("topups:catalog")

    operator = _find_operator(draft["operator_id"])
    product = _find_product(operator, product_id) if operator else None
    if product is None:
        messages.error(request, "Ese producto ya no esta disponible.")
        return redirect("topups:catalog")

    # Monto: fijo del producto, o capturado si el producto es de monto libre.
    if product.get("amount_cents"):
        amount = Money(int(product["amount_cents"]))
    else:
        try:
            amount = Money.parse(Decimal(amount_raw))
        except (InvalidOperation, ValueError, ArithmeticError):
            messages.error(request, "Captura un monto valido.")
            return redirect("topups:catalog")

    # Vista previa de la comision, calculada por el servicio de Pagos.
    try:
        preview = payments_client().post(
            "/api/v1/commissions/preview/",
            payload={
                "store_id": str(request.store.id),
                "organization_id": str(request.store.organization_id),
                "service_kind": "TOPUP",
                "product_code": product.get("id", ""),
                "base_cents": amount.cents,
            },
        )
        quote = preview.data or {}
    except ProviderError as exc:
        log.warning("commission_preview_failed", error=exc.message)
        messages.error(
            request,
            "No pudimos calcular la comision en este momento. Intenta de nuevo.",
        )
        return redirect("topups:catalog")

    request.session["topup_draft"] = {
        **draft,
        "product_id": product_id,
        "product_label": product.get("label", ""),
        "amount_cents": amount.cents,
    }

    phone = normalize_mx_phone(draft["phone"])
    return render(
        request,
        "topups/confirm.html",
        {
            "operator": operator,
            "product": product,
            "phone": phone,
            "base_cents": quote.get("base_cents", amount.cents),
            "commission_cents": quote.get("commission_cents", 0),
            "total_cents": quote.get("total_cents", amount.cents),
            "base_display": _money(quote.get("base_cents", amount.cents)),
            "commission_display": _money(quote.get("commission_cents", 0)),
            "total_display": _money(quote.get("total_cents", amount.cents)),
            "rule_description": quote.get("rule_description", ""),
        },
    )


# ---------------------------------------------------------------------------
# 4. Crear la operacion y cobrar
# ---------------------------------------------------------------------------

@login_required
@require_perm("operation.create")
@require_POST
def create_order(request: HttpRequest) -> HttpResponse:
    """Registra la recarga y crea la orden. Todavia NO recarga nada.

    Orden de las llamadas, que importa:
      1. Se registra la recarga en Topups (queda en PENDING_PAYMENT).
      2. Se crea la orden en Payments, apuntando a esa recarga.
    Si el paso 2 falla, queda una recarga huerfana que nunca se ejecuta
    (porque nunca habra un order.paid para ella). Es el fallo seguro.
    """
    draft = request.session.get("topup_draft") or {}
    if not all(k in draft for k in ("product_id", "phone", "amount_cents")):
        messages.error(request, "La operacion expiro. Vuelve a empezar.")
        return redirect("topups:catalog")

    store = request.store
    idem = uuid.uuid4().hex

    try:
        fulfillment = topups_client().post(
            "/api/v1/topups/",
            payload={
                "organization_id": str(store.organization_id),
                "store_id": str(store.id),
                "requested_by_id": str(request.user.id),
                "product_id": draft["product_id"],
                "phone": draft["phone"],
                "amount_cents": int(draft["amount_cents"]),
            },
            idempotency_key=f"topup-{idem}",
        )
        fulfillment_data = fulfillment.data or {}

        order = payments_client().post(
            "/api/v1/orders/",
            payload={
                "organization_id": str(store.organization_id),
                "store_id": str(store.id),
                "store_code": store.code,
                "created_by_id": str(request.user.id),
                "created_by_email": request.user.email,
                "service_kind": "TOPUP",
                "description": (
                    f"{draft.get('operator_name', 'Recarga')} "
                    f"{draft.get('product_label', '')}"
                ).strip()[:200],
                "product_code": draft["product_id"],
                "fulfillment_id": fulfillment_data.get("id"),
                "base_cents": int(draft["amount_cents"]),
                "currency": "MXN",
                "metadata": {"phone_masked": fulfillment_data.get("phone_masked", "")},
            },
            idempotency_key=f"order-{idem}",
        )
        order_data = order.data or {}
    except ProviderNotConfigured as exc:
        audit.record(
            request,
            AuditAction.ORDER_CREATED,
            metadata={"result": "provider_not_configured", "detail": exc.message},
        )
        return render(
            request,
            "topups/provider_unavailable.html",
            {
                "message": exc.message,
                "missing_requirements": list(exc.missing_requirements),
            },
            status=503,
        )
    except ProviderError as exc:
        log.error("topup_order_creation_failed", error=exc.message, code=exc.code)
        messages.error(request, exc.message)
        return redirect("topups:catalog")

    audit.record(
        request,
        AuditAction.ORDER_CREATED,
        object_type="Order",
        object_id=order_data.get("id", ""),
        new_state=order_data.get("state", ""),
        metadata={
            "folio": order_data.get("folio", ""),
            "service_kind": "TOPUP",
            "total_cents": order_data.get("total_cents"),
        },
    )

    request.session.pop("topup_draft", None)
    return redirect("operations:pay", order_id=order_data["id"])


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------

def _find_operator(operator_id) -> dict | None:
    """Busca un operador en el catalogo del servicio de recargas."""
    try:
        response = topups_client().get("/api/v1/catalog/")
    except ProviderError:
        return None
    for operator in (response.data or {}).get("operators", []):
        if str(operator.get("id")) == str(operator_id):
            return operator
    return None


def _find_product(operator: dict | None, product_id: str) -> dict | None:
    if not operator:
        return None
    for product in operator.get("products", []):
        if str(product.get("id")) == str(product_id):
            return product
    return None


def _money(cents) -> str:
    return str(Money(int(cents or 0)))
