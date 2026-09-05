"""Dashboard de SAMY Cloud.

Dos vistas segun el rol de la tienda activa:

* **Cajero**: acciones grandes y pocas. Su trabajo es despachar rapido, asi
  que la pantalla se reduce a lo que puede hacer ahora mismo y a lo que ya
  hizo hoy. Nada de graficas ni metricas que no le sirven de pie en el
  mostrador.
* **Dueno**: resumen del dia, ingresos, comisiones y operaciones fallidas.

Regla que se aplica en todo el dashboard: **cuando no hay datos reales, se
muestra un estado vacio explicito**. Nunca cifras de relleno. Un numero
inventado en un panel de dinero destruye la confianza en todo el sistema.

Mientras los microservicios no esten disponibles, los contadores se muestran
como "no disponible" y se dice por que, en vez de mostrar cero, que es un
dato falso: cero significa "no hubo operaciones", no "no pude consultarlo".
"""

from __future__ import annotations

from typing import Any

import structlog
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from apps.gateway.clients import payments_client
from apps.tenancy.models import Role
from samy_common.providers.exceptions import ProviderError

log = structlog.get_logger("dashboard")


@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Punto de entrada tras el login. Enruta segun el rol."""
    membership = request.membership

    if membership is None:
        if request.user.is_platform_admin:
            return render(request, "dashboard/platform_home.html")
        return render(request, "dashboard/no_store.html", status=200)

    if membership.role == Role.CASHIER:
        return _cashier_dashboard(request)
    return _owner_dashboard(request)


def _cashier_dashboard(request: HttpRequest) -> HttpResponse:
    """Pantalla del cajero: acciones grandes, cero ruido."""
    summary = _fetch_daily_summary(request, scope="own")
    return render(
        request,
        "dashboard/cashier.html",
        {
            "summary": summary,
            "actions": _available_actions(request),
        },
    )


def _owner_dashboard(request: HttpRequest) -> HttpResponse:
    """Pantalla del dueno: operacion del dia e ingresos."""
    summary = _fetch_daily_summary(request, scope="store")
    return render(
        request,
        "dashboard/owner.html",
        {
            "summary": summary,
            "actions": _available_actions(request),
        },
    )


def _available_actions(request: HttpRequest) -> list[dict[str, Any]]:
    """Accesos del dashboard.

    Solo se listan modulos que existen y a los que el rol tiene acceso. No se
    pintan botones que no llevan a ningun lado: un boton muerto es una promesa
    incumplida y en este producto no hay ninguno.
    """
    return [
        {
            "key": "topups",
            "label": "Recargas",
            "description": "Tiempo aire y paquetes",
            "url_name": "topups:catalog",
            "icon": "phone",
            "primary": True,
        },
        {
            "key": "billpay",
            "label": "Pago de servicios",
            "description": "CFE, agua y mas",
            "url_name": "billpay:catalog",
            "icon": "receipt",
            "primary": True,
        },
        {
            "key": "history",
            "label": "Historial",
            "description": "Operaciones y comprobantes",
            "url_name": "operations:history",
            "icon": "clock",
            "primary": False,
        },
    ]


def _fetch_daily_summary(request: HttpRequest, *, scope: str) -> dict[str, Any]:
    """Consulta el resumen del dia al microservicio de Pagos.

    Devuelve ``available: False`` si el servicio no responde. La plantilla
    muestra entonces un estado degradado explicito en vez de ceros, porque
    "no pude consultar" y "no hubo operaciones" son cosas distintas y
    confundirlas en un panel de dinero es inaceptable.
    """
    store = request.store
    if store is None:
        return {"available": False, "reason": "sin_tienda"}

    params = {"store_id": str(store.id), "scope": scope}
    if scope == "own":
        params["user_id"] = str(request.user.id)

    try:
        response = payments_client().get("/api/v1/orders/summary/today/", params=params)
    except ProviderError as exc:
        log.warning(
            "daily_summary_unavailable",
            store_id=str(store.id),
            error=exc.message,
            code=exc.code,
        )
        return {"available": False, "reason": "servicio_no_disponible"}

    data = response.data or {}
    return {"available": True, **data}


@require_GET
@cache_control(max_age=86400)
def manifest(request: HttpRequest) -> JsonResponse:
    """Manifiesto PWA.

    Permite instalar SAMY Cloud desde el navegador del celular y que se abra
    a pantalla completa, sin barra de direcciones. Para un cajero eso es la
    diferencia entre "una pagina web" y "la app de la tienda".
    """
    from django.templatetags.static import static

    return JsonResponse(
        {
            "name": "SAMY Cloud",
            "short_name": "SAMY",
            "description": "Recargas, pago de servicios y cobros para tu negocio.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait-primary",
            "background_color": "#ffffff",
            "theme_color": "#ffffff",
            "lang": "es-MX",
            "dir": "ltr",
            "icons": [
                {
                    "src": static("brand/samy-mark.svg"),
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any",
                },
                {
                    "src": static("brand/icon-192.png"),
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": static("brand/icon-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        content_type="application/manifest+json",
    )
