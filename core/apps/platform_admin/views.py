"""Panel de administracion de SAMY Cloud (la plataforma, no la tienda).

Solo accesible con ``platform.*``. Muestra el estado REAL de todo: si una
integracion no esta configurada, lo dice con la lista exacta de lo que falta.
Es la pantalla que hace visible la honestidad del sistema.
"""

from __future__ import annotations

import structlog
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.audit.models import AuditEvent
from apps.gateway.clients import billpay_client, payments_client, topups_client
from apps.tenancy.models import Organization, Store
from apps.tenancy.permissions import require_perm
from samy_common.providers.exceptions import ProviderError

log = structlog.get_logger("platform_admin")


@login_required
@require_perm("platform.view_all_stores")
@require_GET
def home(request):
    services = _services_health()
    return render(
        request,
        "platform/home.html",
        {
            "organizations": Organization.objects.count(),
            "stores": Store.objects.filter(is_active=True).count(),
            "services": services,
            "all_healthy": all(s["reachable"] for s in services.values()),
        },
    )


@login_required
@require_perm("platform.manage_providers")
@require_GET
def providers(request):
    """Estado de cada adaptador y que le falta para operar."""
    result = {}
    for name, client in _clients().items():
        try:
            data = client.get("/api/v1/providers/").data or {}
            result[name] = {"reachable": True, "providers": data.get("providers", [])}
        except ProviderError as exc:
            result[name] = {"reachable": False, "error": exc.message, "providers": []}

    total = sum(len(v["providers"]) for v in result.values())
    ready = sum(
        1
        for v in result.values()
        for p in v["providers"]
        if p.get("status") == "READY"
    )

    return render(
        request,
        "platform/providers.html",
        {"services": result, "total_providers": total, "ready_providers": ready},
    )


@login_required
@require_perm("platform.view_all_stores")
@require_GET
def stores(request):
    return render(
        request,
        "platform/stores.html",
        {
            "stores": Store.objects.select_related("organization")
            .prefetch_related("memberships")
            .order_by("organization__name", "name")
        },
    )


@login_required
@require_perm("platform.view_audit")
@require_GET
def audit_log(request):
    events = AuditEvent.objects.select_related("actor", "store")[:200]
    if action := request.GET.get("action"):
        events = AuditEvent.objects.filter(action=action).select_related(
            "actor", "store"
        )[:200]
    return render(
        request,
        "platform/audit.html",
        {"events": events, "current_action": request.GET.get("action", "")},
    )


def _clients():
    return {
        "payments": payments_client(),
        "topups": topups_client(),
        "billpay": billpay_client(),
    }


def _services_health() -> dict[str, dict]:
    result = {}
    for name, client in _clients().items():
        try:
            data = client.get("/health/ready/").data or {}
            result[name] = {"reachable": True, "status": data.get("status", "ok")}
        except ProviderError as exc:
            result[name] = {"reachable": False, "status": "unreachable", "error": exc.message}
    return result


@login_required
@require_perm("platform.manage_providers")
@require_GET
def catalogo_recargas(request):
    """Catalogo comercial con todo el detalle tecnico.

    Es la contraparte de la pantalla de caja. Alli se oculta todo lo del
    proveedor porque al cajero no le sirve; aqui se muestra todo porque es
    exactamente lo que hace falta para saber POR QUE algo no se puede vender:
    fuente oficial, folio tarifario, fecha de verificacion, version, mapping,
    proveedor, ambiente y motivo del bloqueo.

    El permiso es ``platform.manage_providers`` y no uno de tienda: un cajero
    no puede tocar mappings, y un dueno de tienda tampoco puede inventarse un
    provider_product_id vendible. Decidir por donde sale el dinero es de la
    plataforma.
    """
    try:
        respuesta = topups_client().get("/api/v1/commercial/admin/")
        datos = respuesta.data or {}
        error = ""
    except ProviderError as exc:
        log.warning("catalogo_admin_no_disponible", error=exc.message)
        datos, error = {}, exc.message

    productos = datos.get("products", [])

    # Se agrupa por motivo de bloqueo: la pregunta util no es "cuantos hay"
    # sino "que falta para que se puedan vender".
    bloqueos: dict[str, int] = {}
    for p in productos:
        if not p["sellable"]:
            bloqueos[p["status_label"]] = bloqueos.get(p["status_label"], 0) + 1

    return render(
        request,
        "platform/catalogo.html",
        {
            "products": productos,
            "environment": datos.get("environment", ""),
            "ready_providers": datos.get("ready_providers", []),
            "totals": datos.get("totals", {}),
            "bloqueos": sorted(bloqueos.items(), key=lambda kv: -kv[1]),
            "error": error,
        },
    )
