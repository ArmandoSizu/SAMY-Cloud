# Busca secretos reales dentro de lo que esta a punto de subirse a Git.
#
# POR QUE EXISTE
# --------------
# El repositorio es PUBLICO y el .env de este equipo tiene credenciales de
# proveedores que mueven dinero. "Revisar antes de subir" es una intencion;
# esto es una comprobacion. La diferencia se nota el dia que se sube con prisa.
#
# COMO DECIDE QUE ES UN SECRETO
# -----------------------------
# No usa una lista de patrones "que parecen secretos": usa los valores reales
# del .env local, que es la unica forma de no depender de adivinar su forma.
#
# Pero no todos: descarta los que ya estan, con ese mismo valor, en el
# .env.example **tal como esta en HEAD**. Eso es exacto y no aproximado. Un
# valor que ya vive en un archivo publico y versionado no es un secreto:
# ENVIRONMENT=development, la URL publica de un proveedor o la contrasena de
# juguete de la base de datos de desarrollo. Sin esa regla el escaner reporta
# cuarenta hallazgos inocuos, y un escaner que reporta cuarenta hallazgos
# inocuos deja de leerse -que es la unica forma de que uno real se cuele.
#
# Se lee de HEAD y no del archivo en disco a proposito: si alguien acaba de
# pegar una credencial real dentro del .env.example, ese valor NO esta en HEAD,
# no entra en la lista de publicos y se reporta. Leer el archivo de trabajo
# habria hecho que pegar un secreto ahi lo volviera invisible.
#
# Ademas busca cuatro formas que son secretos por su forma, esten o no en el
# .env: llaves privadas PEM, tokens JWT, cabeceras Authorization con valor y
# URLs con usuario:contrasena embebidos.
#
# QUE NUNCA HACE
# --------------
# NUNCA imprime un valor. Si encuentra algo dice el NOMBRE de la variable y el
# ARCHIVO, y nada mas. Un escaner que para demostrar el hallazgo escribe el
# secreto en la consola -y de ahi al historial de la terminal, y de ahi a un
# pegado en un chat- es el mismo problema que venia a evitar.
#
# USO
#   .\scripts\escanear-secretos.ps1              # revisa lo que Git ya versiona
#   .\scripts\escanear-secretos.ps1 -SoloStaged  # revisa solo el area de stage
#
# Codigo de salida 0 = limpio, 1 = hay al menos un hallazgo.

param(
    [switch]$SoloStaged
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

function Leer-Pares {
    param([string[]]$Lineas)
    $pares = @{}
    foreach ($linea in $Lineas) {
        if (-not $linea) { continue }
        if ($linea -match '^\s*#') { continue }
        $pos = $linea.IndexOf('=')
        if ($pos -lt 1) { continue }
        $clave = $linea.Substring(0, $pos).Trim()
        $valor = $linea.Substring($pos + 1).Trim().Trim('"').Trim("'")
        if ($valor) { $pares[$clave] = $valor }
    }
    return $pares
}

# --- 1. valores publicos: los del .env.example tal como esta en HEAD --------

$publicos = @{}
$ejemploEnHead = @()
try { $ejemploEnHead = @(git show 'HEAD:.env.example' 2>$null) } catch { $ejemploEnHead = @() }
foreach ($par in (Leer-Pares -Lineas $ejemploEnHead).GetEnumerator()) {
    $publicos[$par.Value] = $true
}

# --- 2. valores que no deben aparecer, tomados del .env --------------------

$secretos = @{}
$descartados = 0
if (Test-Path -LiteralPath '.env') {
    foreach ($par in (Leer-Pares -Lineas (Get-Content -LiteralPath '.env')).GetEnumerator()) {
        # Ocho caracteres es el corte por abajo: por debajo hay 'true', '5432'
        # y nombres de servicio, y buscarlos daria aciertos inutiles.
        if ($par.Value.Length -lt 8) { continue }
        if ($publicos.ContainsKey($par.Value)) { $descartados++; continue }
        # Una URL sin usuario:clave no es un secreto, es una direccion. La de
        # produccion de un proveedor esta en su documentacion publica y aparece
        # -con razon- en nuestro codigo, en las pruebas y en los documentos.
        # Vigilarla convierte cada corrida en tres hallazgos que hay que
        # descartar a mano. Si alguna vez una URL SI trae credenciales dentro,
        # la cazan los patrones de la seccion 4, que para eso existen.
        if ($par.Value -match '^[a-z][a-z0-9+.-]*://[^/\s:@]+(/|:\d|$)') { $descartados++; continue }
        $secretos[$par.Key] = $par.Value
    }
} else {
    Write-Host "AVISO: no hay .env local; solo se revisan los patrones genericos." -ForegroundColor Yellow
}

# --- 3. archivos a revisar -------------------------------------------------

if ($SoloStaged) {
    $archivos = git diff --cached --name-only --diff-filter=ACM
} else {
    $archivos = git ls-files
}
$archivos = @($archivos | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })

# --- 4. patrones que son secretos por su forma -----------------------------

# El grupo 'cred' aisla la credencial dentro de la coincidencia. Sirve para
# distinguir un secreto de un marcador de posicion: la documentacion escribe
# 'Bearer TU_LLAVE_PRIVADA' y 'postgres://user:PASSWORD@host', y reportar eso
# en cada corrida entrena a quien lo lee para ignorar la salida completa.
$patrones = @(
    @{ nombre = 'llave privada PEM';      regex = '-----BEGIN [A-Z ]*PRIVATE KEY-----' },
    @{ nombre = 'token JWT';              regex = 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.' },
    @{ nombre = 'cabecera Authorization'; regex = '(?i)authorization["'':\s]+(?:bearer|basic)\s+(?<cred>[A-Za-z0-9._\-+/=]{12,})' },
    @{ nombre = 'URL con usuario:clave';  regex = '(?i)(?:https?|postgres(?:ql)?|redis|amqp)://(?<usuario>[^/\s:@]+):(?<cred>[^/\s@]{4,})@' }
)

function Es-Marcador {
    param([string]$Credencial)
    if (-not $Credencial) { return $false }
    # Sin una sola minuscula no es una credencial real: es TU_LLAVE_PRIVADA,
    # PASSWORD, CAMBIAME o <TOKEN>. Las llaves de verdad de los proveedores que
    # usa este proyecto -Conekta 'key_...', JWT 'eyJ...'- siempre traen
    # minusculas, asi que la regla no puede dejar pasar una.
    return ($Credencial -cmatch '^[A-Z0-9_.<>{}$\-]+$')
}

$hallazgos = @()

foreach ($archivo in $archivos) {
    $texto = Get-Content -LiteralPath $archivo -Raw -ErrorAction SilentlyContinue
    if (-not $texto) { continue }

    foreach ($clave in $secretos.Keys) {
        if ($texto.Contains($secretos[$clave])) {
            $hallazgos += [pscustomobject]@{
                archivo = $archivo
                motivo  = "contiene el valor de $clave"
            }
        }
    }

    foreach ($p in $patrones) {
        foreach ($m in ([regex]$p.regex).Matches($texto)) {
            if (Es-Marcador -Credencial $m.Groups['cred'].Value) { continue }
            # 'postgres://samy:samy@localhost' es el valor por omision de
            # desarrollo. Una contrasena identica al usuario no es una
            # credencial: es un hueco con forma de credencial.
            if ($m.Groups['usuario'].Success -and
                $m.Groups['cred'].Value -ceq $m.Groups['usuario'].Value) { continue }

            # El texto que coincidio puede ser un valor publico: la URL de
            # desarrollo con usuario y contrasena de juguete que ya esta en el
            # .env.example versionado. Se comprueba antes de reportar.
            $esPublico = $false
            foreach ($valor in $publicos.Keys) {
                if ($valor.Length -ge 8 -and ($m.Value.Contains($valor) -or $valor.Contains($m.Value))) {
                    $esPublico = $true
                    break
                }
            }
            if (-not $esPublico) {
                $hallazgos += [pscustomobject]@{ archivo = $archivo; motivo = $p.nombre }
                break
            }
        }
    }
}

# --- 5. veredicto ----------------------------------------------------------

Write-Host ""
Write-Host "ESCANEO DE SECRETOS" -ForegroundColor Cyan
Write-Host ("Archivos revisados         : " + $archivos.Count)
Write-Host ("Valores del .env vigilados : " + $secretos.Count)
Write-Host ("Descartados por ser publicos: " + $descartados + " (mismo valor en HEAD:.env.example)")

# .env fuera de Git no es un detalle: es la unica razon por la que los valores
# vigilados no estan ya publicados.
#
# Se usa 'git ls-files .env' a secas y no '--error-unmatch': esa bandera
# escribe en stderr cuando el archivo NO esta versionado -o sea, en el caso
# bueno- y PowerShell con ErrorActionPreference='Stop' aborta el script justo
# antes de imprimir el veredicto. Un escaner que se cae cuando todo esta bien
# se lee como un escaner roto y se deja de correr.
$envVersionado = @(git ls-files '.env')
if ($envVersionado.Count -gt 0) {
    $hallazgos += [pscustomobject]@{ archivo = '.env'; motivo = 'EL .env ESTA VERSIONADO EN GIT' }
}

Write-Host ""
if ($hallazgos.Count -eq 0) {
    Write-Host "LIMPIO: ningun secreto del .env aparece en los archivos versionados." -ForegroundColor Green
    exit 0
}

Write-Host "HALLAZGOS (no se imprime ningun valor):" -ForegroundColor Red
foreach ($h in $hallazgos) {
    Write-Host ("  " + $h.archivo + "  ->  " + $h.motivo) -ForegroundColor Red
}
Write-Host ""
Write-Host "NO subas nada hasta resolverlo." -ForegroundColor Red
exit 1
