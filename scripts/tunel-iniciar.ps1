# =============================================================================
# Tunel publico TEMPORAL hacia el proxy local, para recibir webhooks.
# =============================================================================
#
# Se ejecuta cloudflared DENTRO de Docker, en la misma red que el resto:
#
#   * no se instala nada en la maquina;
#   * no se publica ningun puerto nuevo (habla con proxy:8000 por la red
#     interna, que ya existe);
#   * se borra con un solo comando: scripts\tunel-detener.ps1
#
# El tunel expone el proxy, y del proxy solo /webhooks/conekta/ llega a
# payments. El resto de su API sigue sin ruta publica.
#
# La URL es efimera: cambia en cada arranque. Hay que volver a registrarla en
# Conekta cada vez que se reinicie.
# =============================================================================

$ErrorActionPreference = "Continue"

docker rm -f samy-tunel 2>&1 | Out-Null

docker run -d --name samy-tunel `
    --network samy-cloud_samy-internal `
    cloudflare/cloudflared:latest `
    tunnel --no-autoupdate --url http://proxy:8000 | Out-Null

Write-Host "Esperando a que el tunel publique su URL..."
$url = $null
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 3
    $logs = docker logs samy-tunel 2>&1 | Out-String
    $m = [regex]::Match($logs, 'https://[a-z0-9-]+\.trycloudflare\.com')
    if ($m.Success) { $url = $m.Value; break }
}

if (-not $url) {
    Write-Host "No se obtuvo URL. Ultimas lineas del tunel:" -ForegroundColor Red
    docker logs samy-tunel 2>&1 | Select-Object -Last 20
    exit 1
}

Write-Host ""
Write-Host "URL PUBLICA: $url" -ForegroundColor Green
Write-Host "ENDPOINT   : $url/webhooks/conekta/" -ForegroundColor Green
$url | Set-Content -Path (Join-Path (Split-Path -Parent $PSScriptRoot) ".tunel-url") -NoNewline

Write-Host ""
Write-Host "=== COMPROBACION: el endpoint responde desde internet ===" -ForegroundColor Cyan
Write-Host "Sin firma debe dar 400 (la vista corre y rechaza):"
try {
    $r = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$url/webhooks/conekta/" `
         -Body '{"id":"evt_sin_firma","type":"order.paid"}' -ContentType "application/json" -ErrorAction Stop
    Write-Host "  HTTP $($r.StatusCode)"
} catch {
    $c = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 'sin respuesta' }
    Write-Host "  HTTP $c"
}
