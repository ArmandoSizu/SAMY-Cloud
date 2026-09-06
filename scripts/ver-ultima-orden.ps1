$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "=== RECARGA 0b5c5c62 (la que se pago en efectivo) ===" -ForegroundColor Cyan
docker compose exec -T topups python manage.py shell -c @"
from apps.fulfillment.models import TopupFulfillment
f = TopupFulfillment.objects.get(pk='0b5c5c62-fa8c-4c66-94a0-fb1b6ec73737')
print(f'estado            = {f.state}')
print(f'order_id          = {f.order_id}')
print(f'operador          = {f.operator_name}')
print(f'producto          = {f.product_label}')
print(f'telefono          = {f.phone_masked}')
print(f'monto             = {f.amount_cents} centavos {f.currency}')
print(f'proveedor         = {f.provider_slug}  modo={f.provider_mode}')
print(f'ref_proveedor     = {f.provider_reference}')
print(f'ref_operador      = {f.operator_reference}')
print(f'intentos          = {f.attempts}')
print(f'idempotency_key   = {f.idempotency_key}')
print()
print('--- historial de estados ---')
for t in f.transitions.all().order_by('created_at'):
    print(f'  {t.created_at:%H:%M:%S}  {t.previous_state or \"(nuevo)\"} -> {t.new_state}   {t.reason}')
"@

Write-Host ""
Write-Host "=== ORDEN e7620690 EN payments ===" -ForegroundColor Cyan
docker compose exec -T payments python manage.py shell -c @"
from apps.orders.models import Order
o = Order.objects.get(pk='e7620690-24d5-4b07-97f4-f1cf7785ccc1')
print(f'folio  = {o.folio}')
print(f'estado = {o.state}')
print(f'total  = {o.total_cents} centavos {o.currency}')
print(f'tipo   = {o.service_kind}')
for a in o.attempts.all():
    print(f'  pago: {a.provider_slug} {a.status} ref={a.provider_reference!r}')
"@
