$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "=== PETICIONES DEL NAVEGADOR AL BFF (core) alrededor de las 18:54 ===" -ForegroundColor Cyan
docker compose logs --since 2h core 2>&1 |
    Select-String -Pattern 'tarjeta|comprobante|cobrar|51ccbb31' |
    Select-Object -Last 25

Write-Host ""
Write-Host "=== LADO payments: llamadas y duracion ===" -ForegroundColor Cyan
docker compose logs --since 2h payments 2>&1 |
    Select-String -Pattern 'conekta|duration_ms|51ccbb31|orders' |
    Select-Object -Last 25

Write-Host ""
Write-Host "=== WEBHOOKS RECIBIDOS (cualquiera) ===" -ForegroundColor Cyan
docker compose exec -T payments python manage.py shell -c @"
from apps.webhooks.models import ReceivedWebhook
todos = ReceivedWebhook.objects.order_by('-received_at')[:10]
if not todos:
    print('  NINGUNO. Conekta no puede alcanzar http://localhost desde internet.')
for w in todos:
    print(f'  {w.received_at:%H:%M:%S}  {w.provider_slug}  {w.event_type}  {w.result}')
"@
