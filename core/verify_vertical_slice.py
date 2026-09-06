"""Verificacion end-to-end del flujo del dinero.

Se ejecuta DENTRO del contenedor del Core y recorre el camino completo usando
el cliente S2S firmado real, no atajos:

    crear orden -> iniciar cobro -> confirmar efectivo -> comprobante

Ademas comprueba las reglas que no se pueden violar:

  1. Una orden no puede ejecutarse sin estar pagada.
  2. El total siempre cuadra con base + comision.
  3. El reparto de la comision suma exactamente la comision.
  4. Confirmar con menos dinero del total se rechaza.
  5. Repetir la peticion con la misma clave de idempotencia no duplica nada.
  6. Un proveedor sin credenciales rechaza operar (no simula).

Uso:
    docker compose exec core python /app/../scripts/verify_vertical_slice.py

o, mas comodo, montado en el contenedor:
    docker compose exec core python manage.py shell < scripts/verify_vertical_slice.py
"""

from __future__ import annotations

import os
import sys
import uuid

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.conf import settings  # noqa: E402

from apps.gateway.clients import payments_client, topups_client  # noqa: E402
from apps.tenancy.models import Membership, Store  # noqa: E402
from samy_common.money import Money  # noqa: E402
from samy_common.providers.exceptions import ProviderError  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FALLA {label}" + (f"  -> {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n  {title}\n{'=' * 62}")


# ---------------------------------------------------------------------------
section("CONTEXTO")
# ---------------------------------------------------------------------------

store = Store.objects.filter(is_active=True).select_related("organization").first()
if store is None:
    print("  No hay tiendas. Ejecuta: manage.py seed_demo --dev")
    sys.exit(1)

cashier = Membership.objects.filter(store=store, role="CASHIER").select_related("user").first()
if cashier is None:
    print("  No hay cajero. Ejecuta: manage.py seed_demo --dev")
    sys.exit(1)

print(f"  Tienda:  {store.name} ({store.code})")
print(f"  Cajero:  {cashier.user.email}")

payments = payments_client()
topups = topups_client()

# ---------------------------------------------------------------------------
section("1. CREAR ORDEN  (venta del comercio, sin proveedor externo)")
# ---------------------------------------------------------------------------

idem = uuid.uuid4().hex
base = Money.parse("300.00")

order_payload = {
    "organization_id": str(store.organization_id),
    "store_id": str(store.id),
    "store_code": store.code,
    "created_by_id": str(cashier.user.id),
    "created_by_email": cashier.user.email,
    "service_kind": "MERCHANT_SALE",
    "description": "Verificacion end-to-end del flujo del dinero",
    "base_cents": base.cents,
    "currency": "MXN",
}

response = payments.post(
    "/api/v1/orders/", payload=order_payload, idempotency_key=f"verify-{idem}"
)
order = response.data
print(f"  Folio:  {order['folio']}")
print(f"  Base:   {order['base_display']}")
print(f"  Comis.: {order['commission_display']}")
print(f"  Total:  {order['total_display']}")

check("la orden se creo", response.status_code == 201)
check("estado inicial CREATED", order["state"] == "CREATED", order["state"])
check(
    "el total cuadra (base + comision)",
    order["total_cents"] == order["base_cents"] + order["commission_cents"],
)

entry = order.get("commission_entry") or {}
if entry:
    partes = (
        entry["store_share_cents"]
        + entry["platform_share_cents"]
        + entry["provider_share_cents"]
    )
    check(
        "el reparto suma exactamente la comision",
        partes == entry["commission_cents"],
        f"{partes} != {entry['commission_cents']}",
    )

# ---------------------------------------------------------------------------
section("2. IDEMPOTENCIA  (la misma clave no crea dos ordenes)")
# ---------------------------------------------------------------------------

repeat = payments.post(
    "/api/v1/orders/", payload=order_payload, idempotency_key=f"verify-{idem}"
)
check(
    "repetir la peticion devuelve LA MISMA orden",
    repeat.data["id"] == order["id"],
    f"{repeat.data['id']} vs {order['id']}",
)
check("no se creo una segunda orden", repeat.data["folio"] == order["folio"])

# ---------------------------------------------------------------------------
section("3. INICIAR COBRO EN EFECTIVO")
# ---------------------------------------------------------------------------

pay_idem = uuid.uuid4().hex
pay = payments.post(
    f"/api/v1/orders/{order['id']}/pay/",
    payload={
        "store_id": str(store.id),
        "actor_id": str(cashier.user.id),
        "method": "CASH",
    },
    idempotency_key=f"pay-{pay_idem}",
)
check("el cobro se inicio", pay.status_code == 200)
check(
    "estado PAYMENT_PENDING",
    pay.data["state"] == "PAYMENT_PENDING",
    pay.data["state"],
)
check("todavia NO esta pagada", pay.data["is_paid"] is False)

# ---------------------------------------------------------------------------
section("4. EFECTIVO INSUFICIENTE  (debe rechazarse)")
# ---------------------------------------------------------------------------

try:
    payments.post(
        f"/api/v1/orders/{order['id']}/confirm-cash/",
        payload={
            "store_id": str(store.id),
            "actor_id": str(cashier.user.id),
            "amount_tendered_cents": order["total_cents"] - 100,
        },
        idempotency_key=f"short-{uuid.uuid4().hex}",
    )
    check("cobrar de menos se rechaza", False, "lo acepto")
except ProviderError:
    check("cobrar de menos se rechaza", True)

estado = payments.get(
    f"/api/v1/orders/{order['id']}/", params={"store_id": str(store.id)}
).data
check(
    "tras el rechazo la orden NO avanzo",
    estado["state"] == "PAYMENT_PENDING",
    estado["state"],
)

# ---------------------------------------------------------------------------
section("5. CONFIRMAR EFECTIVO  (con cambio)")
# ---------------------------------------------------------------------------

tendered = ((order["total_cents"] // 10000) + 1) * 10000  # redondeo a billete
confirmed = payments.post(
    f"/api/v1/orders/{order['id']}/confirm-cash/",
    payload={
        "store_id": str(store.id),
        "actor_id": str(cashier.user.id),
        "amount_tendered_cents": tendered,
    },
    idempotency_key=f"cash-{uuid.uuid4().hex}",
).data

print(f"  Recibido: {Money(tendered)}")
print(f"  Cambio:   {confirmed.get('change_display', '')}")

# Una venta del comercio no tiene servicio externo que entregar, asi que al
# confirmarse el cobro se completa en el acto. Una recarga o un pago de recibo
# se quedarian en PAID esperando la confirmacion del proveedor.
check(
    "venta del comercio llega a SUCCESS al cobrarse",
    confirmed["state"] == "SUCCESS",
    confirmed["state"],
)
check("is_paid es True", confirmed["is_paid"] is True)
check(
    "el cambio esta bien calculado",
    confirmed.get("change_cents") == tendered - order["total_cents"],
)
check("quedo registrada la hora de pago", bool(confirmed.get("paid_at")))

# ---------------------------------------------------------------------------
section("6. LA REGLA DEL DINERO  (no se ejecuta sin pagar)")
# ---------------------------------------------------------------------------

from samy_common.states import OrderState, can_transition  # noqa: E402

check(
    "CREATED -> PROCESSING esta PROHIBIDO",
    not can_transition(OrderState.CREATED, OrderState.PROCESSING),
)
check(
    "PAYMENT_PENDING -> PROCESSING esta PROHIBIDO",
    not can_transition(OrderState.PAYMENT_PENDING, OrderState.PROCESSING),
)
check(
    "PAID -> PROCESSING si se permite",
    can_transition(OrderState.PAID, OrderState.PROCESSING),
)

# ---------------------------------------------------------------------------
section("7. AISLAMIENTO MULTI-TENANT")
# ---------------------------------------------------------------------------

try:
    payments.get(
        f"/api/v1/orders/{order['id']}/", params={"store_id": str(uuid.uuid4())}
    )
    check("otra tienda no puede ver la orden", False, "la devolvio")
except ProviderError:
    check("otra tienda no puede ver la orden", True)

# ---------------------------------------------------------------------------
section("8. PROVEEDORES  (el estado declarado coincide con lo que hay)")
# ---------------------------------------------------------------------------

# Estas comprobaciones valen TANTO con credenciales como sin ellas. El invariante
# no es "todo esta apagado" (eso solo describe el dia uno), sino que ningun
# adaptador pueda mentir sobre si mismo:
#
#   READY  <=>  no le falta ningun requisito
#
# Un adaptador READY con requisitos pendientes estaria afirmando que opera sin
# tener con que. Un adaptador no-READY sin decir que le falta dejaria al dueno
# sin saber que hacer. Ambos casos son fallas.


def _coherente(p: dict) -> bool:
    falta = bool(p.get("missing_requirements"))
    return (p["status"] == "READY") is not falta


def _incoherentes(lista: list[dict]) -> str:
    return ", ".join(
        f"{p['slug']}={p['status']}(falta:{len(p.get('missing_requirements') or [])})"
        for p in lista
        if not _coherente(p)
    )


providers = payments.get("/api/v1/providers/").data["providers"]
for p in providers:
    print(f"  {p['display_name']:<22} {p['status']}")

cash = next((p for p in providers if p["slug"] == "cash"), None)
conekta = next((p for p in providers if p["slug"] == "conekta"), None)

check("efectivo esta OPERATIVO", bool(cash) and cash["status"] == "READY")
check(
    "ningun proveedor de pagos se declara operativo sin llaves",
    all(_coherente(p) for p in providers),
    _incoherentes(providers),
)
check(
    "Conekta declara su estado y, si no opera, dice que le falta",
    bool(conekta)
    and (
        conekta["status"] == "READY" or bool(conekta.get("missing_requirements"))
    ),
    conekta["status"] if conekta else "ausente",
)

topup_providers = topups.get("/api/v1/providers/").data["providers"]
for p in topup_providers:
    print(f"  {p['display_name']:<22} {p['status']}")
check(
    "ningun proveedor de recargas se declara operativo sin llaves",
    all(_coherente(p) for p in topup_providers),
    _incoherentes(topup_providers),
)
recargas_operativas = [p["slug"] for p in topup_providers if p["status"] == "READY"]

# ---------------------------------------------------------------------------
section("9. CATALOGO  (solo lo que el proveedor devuelve de verdad)")
# ---------------------------------------------------------------------------

catalog = topups.get("/api/v1/catalog/").data
disponible = catalog.get("available") is True

if not disponible:
    # Sin proveedor operativo el catalogo tiene que estar vacio Y explicado.
    check("sin proveedor operativo el catalogo no inventa nada", not recargas_operativas)
    check("el catalogo NO ofrece operadores falsos", not catalog.get("operators"))
    check("explica por que esta vacio", bool(catalog.get("reason")))
    print(f"  Motivo: {catalog.get('reason', '')[:100]}")
else:
    # Con proveedor operativo el catalogo tiene contenido, pero sigue sin poder
    # contener nada que el proveedor no haya devuelto: todo producto tiene que
    # venir en MXN y con un monto utilizable.
    operadores = catalog.get("operators") or []
    check(
        "el catalogo solo se llena si hay un proveedor operativo",
        bool(recargas_operativas),
        "catalogo con datos y ningun proveedor READY",
    )
    check("hay operadores sincronizados", bool(operadores), "lista vacia")

    malas_monedas = sorted(
        {
            prod.get("currency")
            for op in operadores
            for prod in op.get("products") or []
            if prod.get("currency") != "MXN"
        }
    )
    check(
        "todo producto se vende en MXN",
        not malas_monedas,
        f"monedas ajenas: {malas_monedas}",
    )

    def _monto_invalido(prod: dict) -> bool:
        cents = prod.get("amount_cents")
        if cents is None:
            # Monto libre: solo es vendible si el proveedor dio el rango.
            return not (prod.get("min_amount_cents") and prod.get("max_amount_cents"))
        return cents <= 0

    montos_invalidos = [
        f"{op['name']}/{prod['label']}"
        for op in operadores
        for prod in op.get("products") or []
        if _monto_invalido(prod)
    ]
    check(
        "ningun producto tiene monto vacio o cero",
        not montos_invalidos,
        ", ".join(montos_invalidos[:5]),
    )

    sin_productos = [op["name"] for op in operadores if not op.get("products")]
    check(
        "ningun operador se muestra sin productos que vender",
        not sin_productos,
        ", ".join(sin_productos),
    )
    check(
        "el catalogo dice cuando se sincronizo",
        bool(catalog.get("last_synced_at")),
    )
    check(
        "el catalogo se sincronizo contra un SANDBOX",
        str(catalog.get("provider_mode") or "").upper() == "SANDBOX",
        f"modo reportado: {catalog.get('provider_mode')!r}",
    )
    total_productos = sum(len(op.get("products") or []) for op in operadores)
    print(
        f"  {len(operadores)} operadores / {total_productos} productos"
        f"  |  proveedor: {', '.join(recargas_operativas)}"
        f"  |  modo: {catalog.get('provider_mode')}"
    )
    for op in operadores:
        print(f"    - {op['name']:<28} {len(op.get('products') or [])} productos")

# ---------------------------------------------------------------------------
section("RESULTADO")
# ---------------------------------------------------------------------------

print(f"\n  PASADAS: {PASS}    FALLIDAS: {FAIL}\n")
print(f"  Orden de prueba: {order['folio']}")
print(f"  Comprobante:     http://localhost:8000/operaciones/{order['id']}/comprobante/\n")

sys.exit(1 if FAIL else 0)
