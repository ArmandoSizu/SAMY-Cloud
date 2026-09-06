"""Pantalla comercial de recargas.

Tres pasos, encadenados con HTMX, que siguen la conversacion real del
mostrador:

    operador  ->  familia  ->  monto

Un cliente entra y dice "ponme $100 de Telcel". No dice un identificador de
producto, ni un SKU, ni un monto raro con decimales. Esta pantalla existe para
que la aplicacion se parezca a esa frase.

LO QUE EL CAJERO NO VE, Y POR QUE
----------------------------------

De aqui no sale un solo dato del proveedor: ni Reloadly, ni Taecel, ni
identificadores, ni ambiente, ni saldo en dolares. No es por ocultar: es que
no le sirve. Un cajero que ve "reloadly / 1234 / SANDBOX" junto al precio no
sabe que hacer con eso, y si algo sale mal lo unico que consigue es
transmitirle al cliente una confusion que no le corresponde.

Un producto que hoy no se puede ejecutar SI aparece, en gris y con
"Temporalmente no disponible". Esconderlo seria peor: el cajero pensaria que
SAMY Cloud no vende Telcel, cuando lo que pasa es que falta el proveedor.
"""

from __future__ import annotations

import structlog
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from apps.gateway.clients import topups_client
from apps.tenancy.permissions import require_perm
from samy_common.providers.exceptions import ProviderError

log = structlog.get_logger("bff.commercial")


def _catalogo(request: HttpRequest) -> dict:
    """Trae el catalogo comercial del microservicio.

    Si el servicio no responde NO se inventa un catalogo vacio que parezca
    normal: se devuelve la razon para que la pantalla la pueda decir.
    """
    try:
        respuesta = topups_client().get("/api/v1/commercial/")
        return respuesta.data or {}
    except ProviderError as exc:
        log.warning("catalogo_comercial_no_disponible", error=exc.message)
        return {
            "operators": [],
            "environment": "",
            "any_sellable": False,
            "error": (
                "El servicio de recargas no responde. "
                "Intenta de nuevo en unos segundos."
            ),
        }


@login_required
@require_perm("operation.create")
@require_GET
def commercial_catalog(request: HttpRequest) -> HttpResponse:
    """Paso 1: elegir compania. Telcel primero."""
    datos = _catalogo(request)
    return render(
        request,
        "topups/commercial.html",
        {
            "operators": datos.get("operators", []),
            "environment": datos.get("environment", ""),
            "any_sellable": datos.get("any_sellable", False),
            "error": datos.get("error", ""),
        },
    )


@login_required
@require_perm("operation.create")
@require_GET
def commercial_families(request: HttpRequest, operator_code: str) -> HttpResponse:
    """Paso 2: que quiere recargar. Saldo, paquete, datos."""
    datos = _catalogo(request)
    operador = next(
        (o for o in datos.get("operators", []) if o["code"] == operator_code.upper()),
        None,
    )
    if operador is None:
        messages.error(request, "Esa compania ya no esta disponible.")
        return redirect("topups:catalog")

    return render(
        request,
        "topups/commercial_families.html",
        {"operator": operador, "environment": datos.get("environment", "")},
    )


@login_required
@require_perm("operation.create")
@require_GET
def commercial_products(
    request: HttpRequest, operator_code: str, family_code: str
) -> HttpResponse:
    """Paso 3: el monto, con lo que incluye."""
    datos = _catalogo(request)
    operador = next(
        (o for o in datos.get("operators", []) if o["code"] == operator_code.upper()),
        None,
    )
    familia = (
        next(
            (f for f in operador["families"] if f["code"] == family_code.upper()),
            None,
        )
        if operador
        else None
    )

    if operador is None or familia is None:
        messages.error(request, "Ese producto ya no esta disponible.")
        return redirect("topups:catalog")

    return render(
        request,
        "topups/commercial_products.html",
        {
            "operator": operador,
            "family": familia,
            "environment": datos.get("environment", ""),
        },
    )
