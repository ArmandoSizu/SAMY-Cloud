# Corre TODA la bateria de pruebas del proyecto.
#
# Existe porque faltaba un comando unico y por eso las pruebas de la libreria
# compartida llevaban tiempo sin ejecutarse sin que nadie lo notara: viven
# fuera de los cuatro servicios Django y usan pytest, que la imagen de
# ejecucion no trae (y hace bien en no traer: es una imagen de produccion).
#
# Uso:  .\scripts\probar-todo.ps1

$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)

$fallos = @()

foreach ($servicio in @('core', 'payments', 'topups', 'billpay')) {
    Write-Host ""
    Write-Host "===== $servicio =====" -ForegroundColor Cyan
    $salida = docker compose exec -T $servicio python manage.py test 2>&1
    $salida | Select-String -Pattern '^(OK|FAILED|Ran |ERROR:|FAIL:)'
    # El veredicto se lee SOLO de la linea final de unittest. Buscar la
    # palabra suelta daba falsos positivos: los propios registros de las
    # pruebas escriben "FAILED" al ejercitar un pago rechazado.
    if ($salida | Select-String -Pattern '^FAILED \(') { $fallos += $servicio }
}

Write-Host ""
Write-Host "===== samy_common (libreria compartida) =====" -ForegroundColor Cyan
# pytest se instala al vuelo dentro del contenedor: es efimero y no ensucia
# la imagen. Si algun dia se anade una etapa 'dev' al Dockerfile, esta linea
# sobra.
docker compose exec -T payments sh -c "python -c 'import pytest' 2>/dev/null || pip install --quiet pytest==9.1.1" | Out-Null
$salida = docker compose exec -T payments sh -c "cd /libs/samy_common && python -m pytest tests -q" 2>&1
$salida | Select-Object -Last 4
if ($salida | Select-String -Pattern '\d+ (failed|error)') { $fallos += 'samy_common' }

Write-Host ""
if ($fallos.Count -eq 0) {
    Write-Host "TODO VERDE" -ForegroundColor Green
} else {
    Write-Host ("CON FALLOS: " + ($fallos -join ', ')) -ForegroundColor Red
    exit 1
}
