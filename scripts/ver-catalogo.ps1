$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "=== 6. OPERADORES QUE EL PROVEEDOR DEVUELVE REALMENTE PARA MEXICO ===" -ForegroundColor Cyan
docker compose exec -T topups python manage.py shell -c @"
from apps.catalog.models import Operator, TopupProduct, CatalogSyncRun
from samy_common.money import Money

run = CatalogSyncRun.objects.filter(succeeded=True).order_by('-finished_at').first()
print(f'Ultima sync: {run.finished_at:%Y-%m-%d %H:%M}  proveedor={run.provider_slug}  modo={run.provider_mode!r}')
print(f'Operadores del proveedor: guardados={run.operators_found}  omitidos_sin_precio_MXN={run.operators_skipped}  productos={run.products_found}')
print()
for op in Operator.objects.filter(is_active=True).order_by('display_order', 'name'):
    prods = op.products.filter(is_active=True).order_by('amount_cents')
    monedas = sorted({p.currency for p in prods})
    montos = [p.amount_cents for p in prods if p.amount_cents is not None]
    libres = [p for p in prods if p.amount_cents is None]
    print(f'{op.name}')
    print(f'   slug={op.slug}  proveedor={op.provider_slug}  id_en_proveedor={op.provider_operator_id}  pais={op.country_code}')
    print(f'   productos={prods.count()}  monedas={monedas}  datos={op.supports_data_packages}')
    if montos:
        print(f'   rango: {Money(min(montos), monedas[0])} .. {Money(max(montos), monedas[0])}')
    if libres:
        for p in libres:
            print(f'   MONTO LIBRE: {p.label}  {Money(p.min_amount_cents, p.currency)} .. {Money(p.max_amount_cents, p.currency)}')
    for p in prods[:6]:
        etiqueta = str(Money(p.amount_cents, p.currency)) if p.amount_cents is not None else 'libre'
        print(f'      - {etiqueta:>12}  {p.label[:58]}')
    if prods.count() > 6:
        print(f'      ... y {prods.count() - 6} mas')
    print()

print('TOTAL productos activos:', TopupProduct.objects.filter(is_active=True).count())
print('Monedas distintas en todo el catalogo:', sorted(set(TopupProduct.objects.filter(is_active=True).values_list('currency', flat=True))))
"@
