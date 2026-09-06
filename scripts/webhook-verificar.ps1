$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "=== LO QUE LLEGO DE CONEKTA (datos reales) ===" -ForegroundColor Cyan
docker compose exec -T payments python manage.py shell -c @'
import json
from apps.webhooks.models import ReceivedWebhook

for w in ReceivedWebhook.objects.order_by("-received_at")[:3]:
    p = w.payload if isinstance(w.payload, dict) else {}
    print("recibido  :", w.received_at.strftime("%H:%M:%S"))
    print("  event_id:", w.event_id)
    print("  tipo    :", w.event_type)
    print("  livemode:", p.get("livemode"), "  <- False = sandbox")
    print("  result  :", w.result)
    print("  claves  :", sorted(p.keys())[:12])
    print()
'@

Write-Host ""
Write-Host "=== DEDUPLICACION DURABLE: reinsertar un event_id REAL ===" -ForegroundColor Cyan
docker compose exec -T payments python manage.py shell -c @'
from django.db import IntegrityError, transaction
from apps.webhooks.models import ReceivedWebhook

w = ReceivedWebhook.objects.order_by("-received_at").first()
if w is None:
    print("No hay webhooks recibidos.")
else:
    print("Intentando duplicar el event_id real:", w.event_id)
    try:
        with transaction.atomic():
            ReceivedWebhook.objects.create(
                provider_slug=w.provider_slug,
                event_id=w.event_id,
                event_type=w.event_type,
                provider_reference=w.provider_reference,
                outcome=w.outcome,
                payload=w.payload,
            )
        print("  FALLA: la base acepto un duplicado.")
    except IntegrityError:
        print("  OK: la restriccion unica lo rechaza. Sobrevive a reinicios de Redis.")
    print("  Total de filas con ese event_id:",
          ReceivedWebhook.objects.filter(event_id=w.event_id).count())
'@

Write-Host ""
Write-Host "=== PRUEBAS AUTOMATICAS DE WEBHOOK ===" -ForegroundColor Cyan
docker compose exec -T payments python manage.py test apps.webhooks --settings=config.settings.test 2>&1 |
    Select-String -Pattern '^Ran |^OK|FAILED' | Select-Object -Last 3

Write-Host ""
Write-Host "=== CONCILIACION: que estados cubre hoy ===" -ForegroundColor Cyan
Select-String -Path services\payments\apps\orders\tasks.py -Pattern 'state__in|OrderState\.' |
    ForEach-Object { "{0,5}: {1}" -f $_.LineNumber, $_.Line.Trim() }
