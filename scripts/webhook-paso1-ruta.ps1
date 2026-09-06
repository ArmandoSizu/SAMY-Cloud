$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "=== RECARGANDO EL PROXY ===" -ForegroundColor Cyan
docker compose restart proxy | Out-Null
Start-Sleep -Seconds 6

Write-Host ""
Write-Host "=== LA RUTA PUBLICA LLEGA A payments? ===" -ForegroundColor Cyan
Write-Host "Sin firma debe RECHAZARSE (esa es la prueba de que la vista corre):"
try {
    $r = Invoke-WebRequest -UseBasicParsing -Method POST `
         -Uri "http://localhost:8000/webhooks/conekta/" `
         -Body '{"id":"evt_prueba","type":"order.paid"}' `
         -ContentType "application/json" -ErrorAction Stop
    Write-Host "  HTTP $($r.StatusCode)  $($r.Content)"
} catch {
    $resp = $_.Exception.Response
    if ($resp) {
        $codigo = [int]$resp.StatusCode
        $lector = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $cuerpo = $lector.ReadToEnd()
        Write-Host "  HTTP $codigo  $cuerpo"
    } else {
        Write-Host "  Error de red: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "=== EL RESTO DE payments SIGUE INALCANZABLE? ===" -ForegroundColor Cyan
foreach ($ruta in @('/api/v1/orders/', '/api/v1/providers/', '/api/v1/webhooks/conekta/')) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8000$ruta" -ErrorAction Stop
        Write-Host ("  {0,-32} HTTP {1}  <-- revisar" -f $ruta, $r.StatusCode)
    } catch {
        $c = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 'sin respuesta' }
        Write-Host ("  {0,-32} HTTP {1}  (no expuesto)" -f $ruta, $c)
    }
}
