"""API del catalogo comercial.

Dos vistas con audiencias distintas, y la diferencia entre ellas es
deliberada:

* ``catalogo_comercial`` es lo que ve el mostrador. **No lleva un solo dato
  del proveedor**: ni slug, ni identificador, ni ambiente, ni saldo. Al cajero
  no le sirve saber que la recarga sale por Reloadly, y al cliente menos. Un
  producto que hoy no se puede ejecutar aparece, pero marcado, para que el
  cajero no crea que SAMY Cloud no lo tiene.

* ``catalogo_administracion`` es para el panel: ahi si van la fuente oficial,
  el folio tarifario, la fecha de verificacion, la version, el mapping, el
  proveedor, el ambiente y el motivo real del bloqueo.
"""

from __future__ import annotations

from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from apps.commercial import services
from apps.commercial.models import CatalogStatus


@api_view(["GET"])
def catalogo_comercial(request: Request) -> Response:
    """Catalogo agrupado por operador y familia, listo para pintar."""
    listos = services.proveedores_listos()
    ambiente = services.ambiente_actual()
    filas = services.catalogo_comercial(ambiente=ambiente, listos=listos)

    operadores: dict[str, dict] = {}
    for fila in filas:
        op = operadores.setdefault(
            fila["operator_code"],
            {
                "code": fila["operator_code"],
                "name": fila["operator_name"],
                "display_priority": fila["operator_priority"],
                "is_primary": fila["operator_is_primary"],
                "families": {},
            },
        )
        fam = op["families"].setdefault(
            fila["family_code"],
            {
                "code": fila["family_code"],
                "name": fila["family_name"],
                "display_priority": fila["family_priority"],
                "products": [],
            },
        )
        fam["products"].append(
            {
                "id": fila["id"],
                "commercial_name": fila["commercial_name"],
                "price_cents": fila["price_cents"],
                "price_display": fila["price_display"],
                "currency": fila["currency"],
                "validity_days": fila["validity_days"],
                "data_mb": fila["data_mb"],
                "benefits": fila["benefits"],
                "calls": fila["calls"],
                "sms": fila["sms"],
                "sellable": fila["sellable"],
                "unavailable_reason": fila["unavailable_reason"],
            }
        )

    # Se ordena aqui y no en la plantilla: el orden es una regla de negocio
    # (Telcel primero) y tiene que ser el mismo en cualquier cliente.
    salida = sorted(operadores.values(), key=lambda o: (o["display_priority"], o["name"]))
    for op in salida:
        op["families"] = sorted(
            op["families"].values(), key=lambda f: (f["display_priority"], f["name"])
        )
        for fam in op["families"]:
            fam["products"].sort(key=lambda p: p["price_cents"])
        op["sellable_count"] = sum(
            1 for f in op["families"] for p in f["products"] if p["sellable"]
        )

    return Response(
        {
            "operators": salida,
            "environment": ambiente,
            "any_sellable": any(o["sellable_count"] for o in salida),
        }
    )


@api_view(["GET"])
def catalogo_administracion(request: Request) -> Response:
    """El catalogo con todo el detalle tecnico, para el panel."""
    listos = services.proveedores_listos()
    ambiente = services.ambiente_actual()

    filas = []
    for producto in services.productos_para_administracion():
        estado = services.disponibilidad(producto, listos=listos, ambiente=ambiente)
        version = producto.version_vigente()
        filas.append(
            {
                "id": str(producto.id),
                "operator": producto.operator.name,
                "operator_priority": producto.operator.display_priority,
                "family": producto.family.name,
                "commercial_name": producto.commercial_name,
                "price_display": str(producto.price),
                "currency": producto.currency,
                "official_verified": producto.official_verified,
                "official_source": producto.official_source,
                "official_tariff_reference": producto.official_tariff_reference,
                "verified_at": producto.verified_at.isoformat()
                if producto.verified_at
                else "",
                "version": version.version if version else None,
                "stored_status": producto.status,
                "status": estado.estado,
                "status_label": CatalogStatus(estado.estado).label,
                "blocked_reason": "" if estado.vendible else estado.motivo,
                "sellable": estado.vendible,
                "mappings": [
                    {
                        "provider": m.provider_slug,
                        "provider_product_id": m.provider_product_id,
                        "environment": m.environment,
                        "enabled": m.enabled,
                        "status": m.status,
                        "status_reason": m.status_reason,
                        "priority": m.priority,
                        "last_verified_at": m.last_verified_at.isoformat()
                        if m.last_verified_at
                        else "",
                    }
                    for m in producto.mappings.all()
                ],
            }
        )

    filas.sort(key=lambda f: (f["operator_priority"], f["family"], f["price_display"]))
    return Response(
        {
            "products": filas,
            "environment": ambiente,
            "ready_providers": sorted(listos),
            "totals": {
                "all": len(filas),
                "sellable": sum(1 for f in filas if f["sellable"]),
                "blocked": sum(1 for f in filas if not f["sellable"]),
            },
        }
    )
