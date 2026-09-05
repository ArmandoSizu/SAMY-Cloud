"""Flujo de pago de servicios visto desde el Core (BFF).

Estructura identica al flujo de recargas, con dos diferencias propias del
dominio:

1. **La referencia se puede escanear.** El lector de codigos vive en la
   pantalla de captura y siempre convive con la entrada manual: un recibo
   arrugado no se lee y el cajero no puede quedarse bloqueado.

2. **Se consulta el adeudo antes de cobrar**, cuando el agregador lo permite.
   Es la unica validacion que realmente cuenta: la del formato de la
   referencia solo atrapa errores de captura.

Mientras no haya un agregador contratado, ``catalog()`` muestra exactamente
que falta. No se ofrecen servicios que no se pueden pagar.
"""

from __future__ import annotations

import uuid

import structlog
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.gateway.clients import billpay_client, payments_client
from apps.tenancy.permissions import require_perm
from samy_common.money import Money
from samy_common.providers.exceptions import ProviderError, ProviderNotConfigured

log = structlog.get_logger("bff.billpay")


@login_required
@require_perm("operation.create")
@require_GET
def catalog(request: HttpRequest) -> HttpResponse:
    """Servicios pagables. Vacio y explicado si no hay agregador."""
    try:
        response = billpay_client().get("/api/v1/billers/")
        data = response.data or {}
    except ProviderError as exc:
        log.warning("billers_unavailable", error=exc.message)
        data = {
            "billers": [],
            "available": False,
            "reason": "El servicio de pago de recibos no responde.",
            "missing_requirements": [],
        }

    return render(
        request,
        "billpay/catalog.html",
        {
            "billers": data.get("billers", []),
            "available": data.get("available", False),
            "reason": data.get("reason", ""),
            "provider_status": data.get("provider_status", ""),
            "missing_requirements": data.get("missing_requirements", []),
        },
    )


@login_required
@require_perm("operation.create")
@require_GET
def reference_step(request: HttpRequest, biller_id: uuid.UUID) -> HttpResponse:
    """Captura de la referencia: escaner o teclado."""
    biller = _find_biller(biller_id)
    if biller is None:
        messages.error(request, "Ese servicio ya no esta disponible.")
        return redirect("billpay:catalog")

    return render(request, "billpay/reference.html", {"biller": biller})


@login_required
@require_perm("operation.create")
@require_POST
def inquire(request: HttpRequest, biller_id: uuid.UUID) -> HttpResponse:
    """Valida la referencia y consulta el adeudo al agregador."""
    biller = _find_biller(biller_id)
    if biller is None:
        messages.error(request, "Ese servicio ya no esta disponible.")
        return redirect("billpay:catalog")

    reference = (request.POST.get("reference") or "").strip()
    source = request.POST.get("source", "manual")  # 'scan' o 'manual'

    if not reference:
        return render(
            request,
            "billpay/_reference_form.html",
            {"biller": biller, "error": "Captura la referencia del recibo."},
            status=422,
        )

    try:
        response = billpay_client().post(
            "/api/v1/billers/inquire/",
            payload={
                "biller_id": str(biller_id),
                "reference": reference,
                "store_id": str(request.store.id),
                "source": source,
            },
            idempotency_key=f"inq-{uuid.uuid4().hex}",
        )
        inquiry = response.data or {}
    except ProviderNotConfigured as exc:
        return render(
            request,
            "topups/provider_unavailable.html",
            {"message": exc.message, "missing_requirements": list(exc.missing_requirements)},
            status=503,
        )
    except ProviderError as exc:
        return render(
            request,
            "billpay/_reference_form.html",
            {"biller": biller, "error": exc.message, "reference_value": reference},
            status=422,
        )

    if not inquiry.get("found"):
        return render(
            request,
            "billpay/_reference_form.html",
            {
                "biller": biller,
                "error": inquiry.get(
                    "message", "No encontramos un recibo con esa referencia."
                ),
                "reference_value": reference,
            },
            status=422,
        )

    request.session["billpay_draft"] = {
        "biller_id": str(biller_id),
        "biller_name": biller.get("name", ""),
        "reference": inquiry.get("reference", reference),
        "amount_cents": inquiry.get("amount_due_cents", 0),
    }

    return render(
        request,
        "billpay/confirm.html",
        {"biller": biller, "inquiry": inquiry},
    )


@login_required
@require_perm("operation.create")
@require_POST
def create_order(request: HttpRequest) -> HttpResponse:
    """Registra el pago del recibo y crea la orden. Todavia no paga nada."""
    draft = request.session.get("billpay_draft") or {}
    if not draft.get("reference"):
        messages.error(request, "La operacion expiro. Vuelve a empezar.")
        return redirect("billpay:catalog")

    store = request.store
    idem = uuid.uuid4().hex

    try:
        fulfillment = billpay_client().post(
            "/api/v1/payments/",
            payload={
                "organization_id": str(store.organization_id),
                "store_id": str(store.id),
                "requested_by_id": str(request.user.id),
                "biller_id": draft["biller_id"],
                "reference": draft["reference"],
                "amount_cents": int(draft["amount_cents"]),
            },
            idempotency_key=f"bill-{idem}",
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
                "service_kind": "BILL_PAYMENT",
                "description": f"{draft.get('biller_name', 'Servicio')}"[:200],
                "product_code": draft["biller_id"],
                "fulfillment_id": fulfillment_data.get("id"),
                "base_cents": int(draft["amount_cents"]),
                "currency": "MXN",
                "metadata": {
                    "reference_masked": fulfillment_data.get("reference_masked", "")
                },
            },
            idempotency_key=f"order-{idem}",
        )
        order_data = order.data or {}
    except ProviderNotConfigured as exc:
        return render(
            request,
            "topups/provider_unavailable.html",
            {"message": exc.message, "missing_requirements": list(exc.missing_requirements)},
            status=503,
        )
    except ProviderError as exc:
        messages.error(request, exc.message)
        return redirect("billpay:catalog")

    audit.record(
        request,
        AuditAction.ORDER_CREATED,
        object_type="Order",
        object_id=order_data.get("id", ""),
        new_state=order_data.get("state", ""),
        metadata={"folio": order_data.get("folio", ""), "service_kind": "BILL_PAYMENT"},
    )

    request.session.pop("billpay_draft", None)
    return redirect("operations:pay", order_id=order_data["id"])


def _find_biller(biller_id) -> dict | None:
    try:
        response = billpay_client().get("/api/v1/billers/")
    except ProviderError:
        return None
    for biller in (response.data or {}).get("billers", []):
        if str(biller.get("id")) == str(biller_id):
            return biller
    return None
