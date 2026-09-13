# Inserta ENVIRONMENT en los settings base y de prueba de los cuatro
# servicios. Se usa una sola vez; queda en el repo porque documenta
# exactamente que se toco.

$raiz = Split-Path -Parent $PSScriptRoot

$bloqueBase = @'

# ---------------------------------------------------------------------------
# Ambiente de ejecucion
# ---------------------------------------------------------------------------
# Decide contra que puede operar el servicio. La regla se aplica en
# samy_common.providers.environment y se comprueba en ensure_ready(), que es
# la guardia por la que pasa toda operacion que mueve dinero:
#
#     ENVIRONMENT=production   <->  proveedores en modo PRODUCTION
#     cualquier otro ambiente  <->  proveedores en modo SANDBOX
#
# Un valor desconocido NO se interpreta como desarrollo: se rechaza. Y el
# valor por omision es "development", de modo que un despliegue productivo
# que olvide definirlo se niega a usar credenciales de produccion en vez de
# venderlas por error.
ENVIRONMENT = env.str("ENVIRONMENT", default="development")
'@

$destinosBase = @(
    "core\config\settings\base.py",
    "services\payments\config\settings\base.py",
    "services\topups\config\settings\base.py",
    "services\billpay\config\settings\base.py"
)

foreach ($rel in $destinosBase) {
    $ruta = Join-Path $raiz $rel
    $texto = Get-Content -LiteralPath $ruta -Raw
    if ($texto -match '(?m)^ENVIRONMENT = env\.str') {
        Write-Output "ya tenia ENVIRONMENT: $rel"
        continue
    }
    $ancla = 'SERVICE_VERSION = env.str("SERVICE_VERSION", default="0.1.0")'
    if (-not $texto.Contains($ancla)) {
        Write-Output "NO SE ENCONTRO EL ANCLA en $rel"
        continue
    }
    $texto = $texto.Replace($ancla, $ancla + "`n" + $bloqueBase)
    Set-Content -LiteralPath $ruta -Value $texto -NoNewline
    Write-Output "ENVIRONMENT agregado: $rel"
}

$bloqueTest = @'

# Ambiente de PRUEBAS. Es lo que impide que la suite opere contra produccion:
# con este valor, un proveedor en modo PRODUCTION es rechazado por
# ensure_ready() antes de autenticarse, y ninguna prueba puede gastar saldo
# real ni mandar una recarga a un telefono real.
ENVIRONMENT = "test"
'@

$destinosTest = @(
    "core\config\settings\test.py",
    "services\payments\config\settings\test.py",
    "services\topups\config\settings\test.py",
    "services\billpay\config\settings\test.py"
)

foreach ($rel in $destinosTest) {
    $ruta = Join-Path $raiz $rel
    $texto = Get-Content -LiteralPath $ruta -Raw
    if ($texto -match '(?m)^ENVIRONMENT = "test"') {
        Write-Output "ya tenia ENVIRONMENT: $rel"
        continue
    }
    Add-Content -LiteralPath $ruta -Value $bloqueTest
    Write-Output "ENVIRONMENT=test agregado: $rel"
}
