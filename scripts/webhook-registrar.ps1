# =============================================================================
# Registra (o actualiza) el webhook de SAMY Cloud en Conekta SANDBOX.
# =============================================================================
# La llave privada se lee del entorno del contenedor: no aparece en este
# archivo, ni en la linea de comandos, ni en la salida. La URL del tunel no
# lleva ningun secreto.
# =============================================================================

$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

$url = (Get-Content -Raw (Join-Path $raiz ".tunel-url")).Trim()
$endpoint = "$url/webhooks/conekta/"
Write-Host "Endpoint a registrar: $endpoint" -ForegroundColor Cyan

# El endpoint se pasa por variable de entorno, no interpolado en el codigo.
$env:SAMY_WEBHOOK_ENDPOINT = $endpoint

$py = @'
import base64, json, os, sys
import httpx

ENDPOINT = os.environ['SAMY_WEBHOOK_ENDPOINT']
privada = os.environ.get('CONEKTA_PRIVATE_KEY', '')
if not privada:
    print('SIN CREDENCIAL')
    sys.exit(1)

cab = {
    'Accept': 'application/vnd.conekta-v2.3.0+json',
    'Authorization': 'Basic ' + base64.b64encode((privada + ':').encode()).decode(),
    'Content-Type': 'application/json',
}
BASE = 'https://api.conekta.io'

r = httpx.get(BASE + '/webhooks', headers=cab, timeout=25.0)
print('GET /webhooks -> HTTP', r.status_code)
if r.status_code >= 400:
    print(r.text[:400])
    sys.exit(1)

existentes = r.json().get('data', [])
print('Webhooks ya registrados:', len(existentes))
for w in existentes:
    print('   id=', w.get('id'), ' livemode=', w.get('livemode'), ' url=', w.get('url'))

if any(w.get('livemode') for w in existentes):
    print('ALTO: hay webhooks con livemode=true. Es una cuenta de produccion.')
    sys.exit(2)

ya = [w for w in existentes if w.get('url') == ENDPOINT]
if ya:
    print('Ese endpoint ya estaba registrado. No se crea otro.')
    sys.exit(0)

eventos = [
    'order.paid', 'order.expired', 'order.canceled',
    'charge.paid', 'charge.declined', 'charge.refunded',
]
r = httpx.post(BASE + '/webhooks', headers=cab,
               json={'url': ENDPOINT, 'subscribed_events': eventos},
               timeout=30.0)
print('POST /webhooks -> HTTP', r.status_code)
if r.status_code >= 400:
    print(r.text[:600])
    sys.exit(1)

d = r.json()
print()
print('REGISTRADO:')
print('   id       =', d.get('id'))
print('   url      =', d.get('url'))
modo = 'OK (sandbox)' if d.get('livemode') is False else 'REVISAR'
print('   livemode =', d.get('livemode'), ' ', modo)
print('   eventos  =', d.get('subscribed_events'))
print('   status   =', d.get('status'))
'@

$py | docker compose exec -T -e SAMY_WEBHOOK_ENDPOINT=$endpoint payments python -
