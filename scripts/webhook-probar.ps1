$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "=== PIDIENDO A CONEKTA QUE ENVIE UN EVENTO DE PRUEBA ===" -ForegroundColor Cyan
$py = @'
import base64, os, sys
import httpx

privada = os.environ.get('CONEKTA_PRIVATE_KEY', '')
cab = {
    'Accept': 'application/vnd.conekta-v2.3.0+json',
    'Authorization': 'Basic ' + base64.b64encode((privada + ':').encode()).decode(),
    'Content-Type': 'application/json',
}
BASE = 'https://api.conekta.io'

r = httpx.get(BASE + '/webhooks', headers=cab, timeout=25.0)
hooks = r.json().get('data', [])
if not hooks:
    print('No hay webhooks registrados.')
    sys.exit(1)
h = hooks[0]
print('webhook id =', h.get('id'), ' livemode =', h.get('livemode'), ' status =', h.get('status'))

# Conekta expone un disparador de prueba en algunas versiones de la API.
for ruta in ['/webhooks/{}/test', '/webhooks/{}/ping']:
    destino = BASE + ruta.format(h['id'])
    try:
        rr = httpx.post(destino, headers=cab, json={}, timeout=30.0)
    except Exception as exc:
        print(ruta, '-> error de red:', type(exc).__name__)
        continue
    print(ruta, '-> HTTP', rr.status_code)
    if rr.status_code < 400:
        print('   Conekta acepto enviar el evento de prueba.')
        sys.exit(0)
    print('   ', rr.text[:200])

print()
print('La API no ofrece disparador de prueba en esta version.')
sys.exit(3)
'@
$py | docker compose exec -T payments python -

Start-Sleep -Seconds 8

Write-Host ""
Write-Host "=== QUE LLEGO AL ENDPOINT ===" -ForegroundColor Cyan
docker compose logs --tail 60 proxy 2>&1 | Select-String -Pattern 'webhooks/conekta' | Select-Object -Last 6

Write-Host ""
Write-Host "=== WEBHOOKS REGISTRADOS EN LA BASE ===" -ForegroundColor Cyan
docker compose exec -T payments python manage.py shell -c @'
from apps.webhooks.models import ReceivedWebhook
todos = ReceivedWebhook.objects.order_by('-received_at')[:10]
if not todos:
    print('  Ninguno todavia.')
for w in todos:
    print('  ', w.received_at.strftime('%H:%M:%S'), w.provider_slug, w.event_type,
          'id=' + str(w.event_id)[:24], w.result)
'@
