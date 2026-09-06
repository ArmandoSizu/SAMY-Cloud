$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

function Titulo($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }

Titulo "CHECKS"
foreach ($s in @('core','payments','topups','billpay')) {
    $r = docker compose exec -T $s python manage.py check 2>&1 | Select-Object -Last 1
    Write-Host ("  {0,-10} {1}" -f $s, $r)
}

Titulo "CHECKS CON AJUSTES DE PRODUCCION (core)"
docker compose exec -T core python manage.py check --deploy --settings=config.settings.prod 2>&1 |
    Select-String -Pattern 'System check|WARNINGS|ERRORS|\?:' | Select-Object -First 12

Titulo "PRUEBAS"
$r = docker compose exec -T core python -m pytest /libs/samy_common/tests -q 2>&1 | Select-String -Pattern 'passed|failed' | Select-Object -Last 1
Write-Host ("  {0,-12} {1}" -f 'samy_common', ($r -join ' ').Trim())
foreach ($s in @('core','payments','topups')) {
    $extra = if ($s -eq 'core') { 'apps' } else { '' }
    $r = docker compose exec -T $s python manage.py test $extra --settings=config.settings.test 2>&1 |
         Select-String -Pattern '^Ran |^OK$|^FAILED' | Out-String
    Write-Host ("  {0,-12} {1}" -f $s, ($r -replace "`r?`n", ' ').Trim())
}

Titulo "VERIFICACION EXTREMO A EXTREMO"
docker compose exec -T core python verify_vertical_slice.py 2>&1 | Select-String -Pattern 'PASADAS|FALLA '

Titulo "CONTENEDORES"
docker compose ps --format "table {{.Service}}\t{{.Status}}"
