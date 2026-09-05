<#
.SYNOPSIS
    Levanta SAMY Cloud completo en Windows, de cero.

.DESCRIPTION
    Hace en un paso lo que el README describe manualmente:
      1. Crea .env desde la plantilla si no existe
      2. Genera los secretos criptograficos
      3. Levanta los contenedores
      4. Espera a que PostgreSQL este listo de verdad
      5. Aplica migraciones en los CUATRO servicios
      6. Carga datos iniciales
      7. Compila el CSS
      8. Verifica que todo responde

.NOTES
    Requiere Docker Desktop CORRIENDO. Abrelo desde el menu Inicio antes.
#>

[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$Reset
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

function Write-Step { param($m) Write-Host "`n>> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "   OK  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "   !   $m" -ForegroundColor Yellow }

# --- 0. Docker ---------------------------------------------------------------
Write-Step "Verificando Docker"
docker version --format '{{.Server.Version}}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host @"

   Docker no responde.

   Abre Docker Desktop desde el menu Inicio, espera a que el icono de la
   ballena deje de animarse, y vuelve a ejecutar este script.

"@ -ForegroundColor Red
    exit 1
}
Write-Ok "Docker responde"

# --- 1. .env -----------------------------------------------------------------
Write-Step "Configuracion"
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Ok ".env creado desde la plantilla"

    # Secretos reales. Nunca se versionan.
    $secret = python -c "import secrets; print(secrets.token_urlsafe(64))"
    $s2s    = python -c "import secrets; print(secrets.token_urlsafe(64))"
    $pg     = python -c "import secrets; print(secrets.token_urlsafe(24))"

    $content = Get-Content .env -Raw
    $content = $content -replace 'DJANGO_SECRET_KEY=.*', "DJANGO_SECRET_KEY=$secret"
    $content = $content -replace 'SERVICE_S2S_SECRET=.*', "SERVICE_S2S_SECRET=$s2s"
    $content = $content -replace 'POSTGRES_SUPERUSER_PASSWORD=.*', "POSTGRES_SUPERUSER_PASSWORD=$pg"
    Set-Content .env $content -NoNewline
    Write-Ok "Secretos generados"
} else {
    Write-Warn ".env ya existe, no se toca"
}

# --- 2. Reset opcional --------------------------------------------------------
if ($Reset) {
    Write-Step "Eliminando volumenes (se borran TODOS los datos locales)"
    $answer = Read-Host "   Escribe SI para confirmar"
    if ($answer -ne 'SI') { Write-Warn "Cancelado"; exit 0 }
    docker compose down -v
    Write-Ok "Volumenes eliminados"
}

# --- 3. Contenedores ----------------------------------------------------------
Write-Step "Levantando contenedores"
if ($SkipBuild) { docker compose up -d } else { docker compose up -d --build }
if ($LASTEXITCODE -ne 0) { Write-Host "   Fallo docker compose up" -ForegroundColor Red; exit 1 }
Write-Ok "Contenedores arriba"

# --- 4. Esperar a PostgreSQL --------------------------------------------------
Write-Step "Esperando a PostgreSQL"
$ready = $false
for ($i = 1; $i -le 60; $i++) {
    docker compose exec -T postgres pg_isready -U samy -d postgres 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
    if ($i % 5 -eq 0) { Write-Host "   ... $($i*2)s" -ForegroundColor DarkGray }
}
if (-not $ready) { Write-Host "   PostgreSQL no respondio" -ForegroundColor Red; exit 1 }
Write-Ok "PostgreSQL listo"

# --- 5. Migraciones -----------------------------------------------------------
Write-Step "Aplicando migraciones"
foreach ($svc in @('core','payments','topups','billpay')) {
    Write-Host "   $svc..." -ForegroundColor DarkGray
    docker compose exec -T $svc python manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { Write-Host "   Fallaron las migraciones de $svc" -ForegroundColor Red; exit 1 }
}
Write-Ok "Migraciones aplicadas en los 4 servicios"

# --- 6. Datos iniciales -------------------------------------------------------
Write-Step "Datos iniciales"
docker compose exec -T core python manage.py seed_demo
if ($LASTEXITCODE -eq 0) { Write-Ok "Organizacion, tienda y usuarios creados" }
else { Write-Warn "seed_demo fallo o ya se habia ejecutado" }

# --- 7. CSS -------------------------------------------------------------------
Write-Step "Compilando CSS"
docker compose run --rm tailwind sh -c "npm install --no-audit --no-fund && npm run build:css"
if ($LASTEXITCODE -eq 0) { Write-Ok "CSS compilado" } else { Write-Warn "Fallo la compilacion del CSS" }

# --- 8. Verificacion ----------------------------------------------------------
Write-Step "Verificando"
Start-Sleep -Seconds 3
try {
    $r = Invoke-RestMethod -Uri "http://localhost:8000/health/" -TimeoutSec 10
    Write-Ok "Core responde: $($r.status) v$($r.version)"
} catch {
    Write-Warn "El health check no respondio todavia; dale unos segundos mas"
}

docker compose ps

Write-Host @"

================================================================
  SAMY Cloud esta arriba:  http://localhost:8000
================================================================

  Utiles:
    docker compose logs -f core
    docker compose ps
    docker compose down

  Documentacion en docs/
  Estado de las integraciones: docs/api-integrations.md

"@ -ForegroundColor Green
