$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host "=== RECOMPILANDO CSS ===" -ForegroundColor Cyan
docker compose --profile frontend run --rm --no-deps tailwind sh -c "npm install --no-audit --no-fund --silent && npm run build:css" 2>&1 | Select-Object -Last 8

Write-Host ""
Write-Host "=== .conekta-tokenizer presente en el CSS compilado? ===" -ForegroundColor Cyan
$hit = Select-String -Path "core\static\css\app.css" -Pattern 'conekta-tokenizer' | Select-Object -First 1
if ($hit) { Write-Host "  SI (linea $($hit.LineNumber))" } else { Write-Host "  NO -- el CSS no se recompilo" -ForegroundColor Red }
$hit2 = Select-String -Path "core\static\css\app.css" -Pattern 'x-cloak' | Select-Object -First 1
if ($hit2) { Write-Host "  x-cloak: SI" } else { Write-Host "  x-cloak: NO" -ForegroundColor Red }

Write-Host ""
Write-Host "=== REINICIANDO core ===" -ForegroundColor Cyan
docker compose restart core | Out-Null
Start-Sleep -Seconds 12
docker compose exec -T core python manage.py check 2>&1 | Select-Object -Last 2
docker compose ps --format "table {{.Service}}\t{{.Status}}" | Select-String -Pattern 'core|SERVICE'
