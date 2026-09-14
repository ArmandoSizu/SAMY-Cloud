# =============================================================================
# SAMY Cloud - Despliegue en Microsoft Azure
# =============================================================================
#
# Crea la infraestructura y publica SAMY Cloud en App Service for Containers.
#
# POR QUE UN SCRIPT Y NO UNA LISTA DE COMANDOS
# --------------------------------------------
# Son unos treinta comandos de `az`, varios tardan minutos, y el orden importa:
# el App Service necesita la imagen, la imagen necesita el registro, los
# ajustes necesitan el servidor de PostgreSQL. Ejecutarlos a mano, de uno en
# uno, es donde se cuela el paso olvidado -y el paso olvidado aqui es una
# aplicacion que arranca sin base de datos.
#
# Corre desatendido, escribe todo a un archivo de bitacora y se detiene en el
# primer error con el motivo. Es idempotente en lo que importa: comprueba si
# cada recurso existe antes de crearlo, asi que volver a correrlo tras un fallo
# retoma donde se quedo en vez de fracasar por "ya existe".
#
# LO QUE NO HACE
# --------------
# Ninguna recarga. Ni una llamada al endpoint de compra de Linntae. Deja
# ALLOW_REAL_PROVIDER_TRANSACTIONS=true porque el sistema tiene que quedar
# funcional, pero la primera transaccion la hace una persona desde la
# interfaz, con su segunda confirmacion.
#
# USO
#   .\infra\azure\desplegar.ps1
#   .\infra\azure\desplegar.ps1 -Region eastus2      # si la region da problemas
#   .\infra\azure\desplegar.ps1 -SoloImagen          # solo reconstruir y publicar
#
# DONDE CORRERLO
# --------------
# En cualquiera de los dos, y el script se adapta solo:
#
#   a) Azure Cloud Shell. Ya viene autenticado y con az instalado; es el camino
#      corto. Como ahi el repositorio se clona de GitHub y el .env NO se
#      versiona -ni debe-, las credenciales de Linntae se piden por teclado,
#      sin eco.
#
#   b) La maquina de desarrollo, con `az login` hecho EN ELLA. Ahi el script
#      lee las credenciales del .env local y no pregunta nada.
#
# Lo que no funciona es mezclarlos: la sesion de Cloud Shell vive en el
# navegador, dentro de Azure, y el `az` del equipo tiene su propio almacen de
# credenciales, vacio hasta que se hace `az login` ahi.
# =============================================================================

param(
    [string]$Region = "southcentralus",
    [string]$Sufijo = "",
    [switch]$SoloImagen
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)

# El az.cmd por ruta completa: el instalador de winget no refresca el PATH de
# las terminales ya abiertas, y "az no se reconoce" tras instalarlo es el
# primer tropiezo de todos.
$AZ = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
if (-not (Test-Path -LiteralPath $AZ)) {
    $enPath = (Get-Command az -ErrorAction SilentlyContinue).Source
    if ($enPath) { $AZ = $enPath } else { throw "No encuentro el Azure CLI." }
}

$BITACORA = Join-Path $PSScriptRoot "despliegue.log"
"=== $(Get-Date -Format o) ===" | Out-File -LiteralPath $BITACORA -Encoding utf8

function Registrar { param([string]$Texto)
    $linea = "[$(Get-Date -Format HH:mm:ss)] $Texto"
    Write-Host $linea
    $linea | Out-File -LiteralPath $BITACORA -Append -Encoding utf8
}

function Az { # Ejecuta az, registra la salida y aborta con el motivo real.
    param([Parameter(ValueFromRemainingArguments)][string[]]$Argumentos)
    $salida = & $AZ @Argumentos 2>&1
    $salida | Out-File -LiteralPath $BITACORA -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        Registrar "FALLO: az $($Argumentos -join ' ')"
        Registrar ($salida | Out-String)
        throw "az fallo. Ver $BITACORA"
    }
    return ($salida | Out-String).Trim()
}

# -----------------------------------------------------------------------------
# 0. Nombres
# -----------------------------------------------------------------------------
# ACR y Key Vault exigen nombres UNICOS EN TODO AZURE, no solo en la cuenta.
# El sufijo sale del id de la suscripcion: es estable -volver a correr el
# script da los mismos nombres, que es lo que hace que sea idempotente- y a la
# vez es casi seguro que nadie mas lo tenga.
$SUB_ID = Az account show --query id --output tsv
$SUB_NOMBRE = Az account show --query name --output tsv
if (-not $Sufijo) { $Sufijo = $SUB_ID.Replace("-", "").Substring(0, 6).ToLower() }

$RG        = "samy-cloud-rg"
$ACR       = "samycloudacr$Sufijo"
$KV        = "samy-cloud-kv-$Sufijo"
$PG        = "samy-postgres-$Sufijo"
$PLAN      = "samy-cloud-plan"
$APP       = "samy-cloud-$Sufijo"
$IMAGEN    = "samy-cloud:latest"
$PG_USUARIO = "samyadmin"

Registrar "Suscripcion : $SUB_NOMBRE"
Registrar "Region      : $Region"
Registrar "Grupo       : $RG"
Registrar "Registro    : $ACR"
Registrar "PostgreSQL  : $PG"
Registrar "Key Vault   : $KV"
Registrar "Aplicacion  : $APP"

function Existe { param([string[]]$Comprobacion)
    $previo = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $AZ @Comprobacion *> $null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $previo
    return $ok
}

# -----------------------------------------------------------------------------
# 1. Grupo de recursos
# -----------------------------------------------------------------------------
# Todo SAMY Cloud vive aqui y NADA mas vive aqui. Es lo que permite borrarlo
# entero sin tocar el proyecto anterior de la cuenta, y lo que garantiza que
# este script no pueda alcanzarlo.
if (-not $SoloImagen) {
    if (Existe @("group", "show", "--name", $RG)) {
        Registrar "El grupo $RG ya existe; se reutiliza."
    } else {
        Registrar "Creando grupo de recursos..."
        Az group create --name $RG --location $Region --output none
    }
}

# -----------------------------------------------------------------------------
# 2. Azure Container Registry
# -----------------------------------------------------------------------------
# SKU Basic: privado, 10 GB, suficiente de sobra para una imagen de 865 MB.
# No se hace publico en ningun momento.
if (-not (Existe @("acr", "show", "--name", $ACR, "--resource-group", $RG))) {
    Registrar "Creando registro de contenedores (Basic)..."
    Az acr create --resource-group $RG --name $ACR --sku Basic `
        --admin-enabled true --output none
} else {
    Registrar "El registro $ACR ya existe; se reutiliza."
}

# -----------------------------------------------------------------------------
# 3. Construir la imagen EN Azure
# -----------------------------------------------------------------------------
# `az acr build` si acepta --file, al contrario que `gcloud builds submit`, asi
# que aqui no hace falta ningun archivo de configuracion intermedio: se apunta
# al Dockerfile que ya existe.
#
# Construir en Azure y no en local tiene una razon practica: subir una imagen
# de 865 MB por una conexion domestica tarda mucho mas que subir el contexto
# comprimido y construir alla. Y respeta .dockerignore, asi que el .env no
# viaja.
Registrar "Construyendo la imagen en Azure (puede tardar 8-12 min)..."
Az acr build --registry $ACR --image $IMAGEN `
    --file infra/docker/cloudrun.Dockerfile . --output none
Registrar "Imagen publicada: $ACR.azurecr.io/$IMAGEN"

if ($SoloImagen) {
    Registrar "Solo imagen: se omite el resto."
    Registrar "Reiniciando la aplicacion para que tome la imagen nueva..."
    Az webapp restart --name $APP --resource-group $RG --output none
    exit 0
}

# -----------------------------------------------------------------------------
# 4. PostgreSQL Flexible Server
# -----------------------------------------------------------------------------
# Standard_B1ms (Burstable, 1 vCPU, 2 GB) con 32 GB: el SKU mas economico que
# sostiene cuatro esquemas de Django sin quedarse corto. Sin alta
# disponibilidad ni replicas: es una demo.
#
# La contrasena se genera aqui, se guarda en Key Vault y no se imprime nunca.
if (-not (Existe @("postgres", "flexible-server", "show", "--name", $PG, "--resource-group", $RG))) {
    Registrar "Creando PostgreSQL Flexible Server (5-8 min)..."
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $PG_CLAVE = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '').Substring(0, 20) + "aA1"

    Az postgres flexible-server create `
        --resource-group $RG --name $PG --location $Region `
        --admin-user $PG_USUARIO --admin-password $PG_CLAVE `
        --sku-name Standard_B1ms --tier Burstable `
        --storage-size 32 --version 16 `
        --public-access 0.0.0.0 --yes --output none
    Registrar "Servidor creado."
    $GUARDAR_CLAVE = $true
} else {
    Registrar "El servidor $PG ya existe; se reutiliza."
    $GUARDAR_CLAVE = $false
}

# Firewall: se permiten las IP de servicios de Azure, no internet.
#
# 0.0.0.0-0.0.0.0 NO es "abierto a todo" pese a como se lee: es la regla
# especial de Azure que significa "solo servicios de Azure". Sin ella el App
# Service no llega a la base; con un rango real de internet, cualquiera
# llegaria.
Registrar "Configurando el firewall (solo servicios de Azure)..."
if (-not (Existe @("postgres", "flexible-server", "firewall-rule", "show",
                   "--resource-group", $RG, "--name", $PG,
                   "--rule-name", "AllowAzureServices"))) {
    Az postgres flexible-server firewall-rule create `
        --resource-group $RG --name $PG --rule-name AllowAzureServices `
        --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 --output none
}

# -----------------------------------------------------------------------------
# 5. Las cuatro bases, en UN servidor
# -----------------------------------------------------------------------------
# Cuatro bases y no cuatro servidores: la separacion que importa es que ningun
# servicio pueda leer las tablas de otro, y eso lo dan bases distintas. Cuatro
# servidores costarian cuatro veces sin aportar nada.
foreach ($base in @("samy_core", "samy_payments", "samy_topups", "samy_billpay")) {
    if (-not (Existe @("postgres", "flexible-server", "db", "show",
                       "--resource-group", $RG, "--server-name", $PG,
                       "--database-name", $base))) {
        Registrar "Creando base $base..."
        Az postgres flexible-server db create `
            --resource-group $RG --server-name $PG --database-name $base --output none
    } else {
        Registrar "La base $base ya existe."
    }
}

# -----------------------------------------------------------------------------
# 6. Key Vault y secretos
# -----------------------------------------------------------------------------
if (-not (Existe @("keyvault", "show", "--name", $KV, "--resource-group", $RG))) {
    Registrar "Creando Key Vault..."
    Az keyvault create --name $KV --resource-group $RG --location $Region `
        --enable-rbac-authorization true --output none

    # Quien corre el script necesita poder ESCRIBIR secretos, y con RBAC eso no
    # viene dado por ser dueno de la suscripcion.
    $yo = Az ad signed-in-user show --query id --output tsv
    Az role assignment create --assignee $yo `
        --role "Key Vault Secrets Officer" `
        --scope "/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.KeyVault/vaults/$KV" `
        --output none
    Registrar "Esperando a que el permiso se propague (30 s)..."
    Start-Sleep -Seconds 30
} else {
    Registrar "El Key Vault $KV ya existe."
}

function Guardar-Secreto { param([string]$Nombre, [string]$Valor)
    if (-not $Valor) { Registrar "AVISO: $Nombre vino vacio; no se guarda."; return }
    # --value nunca se registra: Az() escribe la salida de az en la bitacora,
    # y la salida de `keyvault secret set` incluye el valor. Por eso este caso
    # NO pasa por Az() y se silencia por completo.
    & $AZ keyvault secret set --vault-name $KV --name $Nombre --value $Valor *> $null
    if ($LASTEXITCODE -ne 0) { throw "No se pudo guardar el secreto $Nombre." }
    Registrar "Secreto $Nombre guardado (valor no registrado)."
}

# Las credenciales de Linntae se leen del .env local y se copian a Key Vault
# sin pasar por pantalla ni por la bitacora. Asi no hay que teclearlas otra
# vez ni pegarlas en ningun chat.
function Leer-Del-Env { param([string]$Clave)
    if (-not (Test-Path -LiteralPath ".env")) { return "" }
    $linea = (Select-String -LiteralPath ".env" -Pattern "^$Clave=" | Select-Object -First 1).Line
    if (-not $linea) { return "" }
    return $linea.Substring($Clave.Length + 1).Trim().Trim('"').Trim("'")
}

if ($GUARDAR_CLAVE) { Guardar-Secreto "DB-PASS" $PG_CLAVE }
# Las credenciales de Linntae vienen del .env cuando este script corre en la
# maquina de desarrollo. En Cloud Shell no hay .env -y no debe haberlo, porque
# ahi el repositorio se clona de GitHub y el .env nunca se versiona-, asi que
# se piden por teclado.
#
# Read-Host -AsSecureString: no se ven al teclearlas, no quedan en el
# historial de la terminal y no pasan por la bitacora. El paso por texto plano
# dura lo que tarda `az` en recibirlas.
#
# Si el secreto YA esta en Key Vault no se vuelve a pedir: volver a correr el
# script tras un fallo no debe obligar a teclear credenciales otra vez.
function Asegurar-Credencial { param([string]$Secreto, [string]$ClaveEnv, [string]$Etiqueta)
    if (Existe @("keyvault", "secret", "show", "--vault-name", $KV, "--name", $Secreto)) {
        Registrar "El secreto $Secreto ya existe en Key Vault."
        return
    }
    $valor = Leer-Del-Env $ClaveEnv
    if ($valor) {
        Registrar "$Secreto tomado del .env local."
    } else {
        Write-Host ""
        Write-Host "  No hay .env en esta maquina. Escribe $Etiqueta de Linntae."
        Write-Host "  No se vera mientras escribes y no queda en el historial."
        $seguro = Read-Host -Prompt "  $Etiqueta" -AsSecureString
        $valor = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro))
    }
    Guardar-Secreto $Secreto $valor
}

Asegurar-Credencial "LINNTAE-USERNAME" "LINNTAE_USERNAME" "usuario"
Asegurar-Credencial "LINNTAE-PASSWORD" "LINNTAE_PASSWORD" "contrasena"

if (-not (Existe @("keyvault", "secret", "show", "--vault-name", $KV, "--name", "DJANGO-SECRET-KEY"))) {
    $b = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    Guardar-Secreto "DJANGO-SECRET-KEY" ([Convert]::ToBase64String($b))
}
if (-not (Existe @("keyvault", "secret", "show", "--vault-name", $KV, "--name", "SERVICE-S2S-SECRET"))) {
    $b = New-Object byte[] 36
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    Guardar-Secreto "SERVICE-S2S-SECRET" ([Convert]::ToBase64String($b))
}

# -----------------------------------------------------------------------------
# 7. App Service
# -----------------------------------------------------------------------------
# B1 (1 vCPU, 1.75 GB) Linux. El nivel gratuito NO sirve: no permite
# contenedores propios y se apaga por inactividad, lo que mataria a Redis y a
# los workers de Celery que viven dentro de la imagen.
if (-not (Existe @("appservice", "plan", "show", "--name", $PLAN, "--resource-group", $RG))) {
    Registrar "Creando plan de App Service (B1 Linux)..."
    Az appservice plan create --name $PLAN --resource-group $RG `
        --location $Region --is-linux --sku B1 --output none
}

if (-not (Existe @("webapp", "show", "--name", $APP, "--resource-group", $RG))) {
    Registrar "Creando la aplicacion web..."
    Az webapp create --resource-group $RG --plan $PLAN --name $APP `
        --deployment-container-image-name "$ACR.azurecr.io/$IMAGEN" --output none

    Registrar "Asignando identidad administrada..."
    Az webapp identity assign --name $APP --resource-group $RG --output none
} else {
    Registrar "La aplicacion $APP ya existe."
}

# La identidad de la aplicacion lee el Key Vault. Es lo que permite que los
# ajustes guarden una REFERENCIA al secreto y no el secreto: el valor no queda
# escrito en la configuracion del App Service, que se ve desde el portal.
$ID_APP = Az webapp identity show --name $APP --resource-group $RG --query principalId --output tsv
Registrar "Concediendo lectura del Key Vault a la aplicacion..."
$previo = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& $AZ role assignment create --assignee $ID_APP `
    --role "Key Vault Secrets User" `
    --scope "/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.KeyVault/vaults/$KV" *> $null
$ErrorActionPreference = $previo

# La aplicacion tambien necesita poder bajar la imagen del registro privado.
Registrar "Configurando el acceso al registro..."
$ACR_USUARIO = Az acr credential show --name $ACR --query username --output tsv
$ACR_CLAVE = (& $AZ acr credential show --name $ACR --query "passwords[0].value" --output tsv)
Az webapp config container set --name $APP --resource-group $RG `
    --container-image-name "$ACR.azurecr.io/$IMAGEN" `
    --container-registry-url "https://$ACR.azurecr.io" `
    --container-registry-user $ACR_USUARIO `
    --container-registry-password $ACR_CLAVE --output none

# -----------------------------------------------------------------------------
# 8. Configuracion
# -----------------------------------------------------------------------------
$HOST_APP = "$APP.azurewebsites.net"
$URL_APP  = "https://$HOST_APP"
$PG_HOST  = "$PG.postgres.database.azure.com"
$VAULT    = "https://$KV.vault.azure.net/secrets"

Registrar "Aplicando la configuracion..."

# WEBSITES_CONTAINER_START_TIME_LIMIT es el ajuste que mas facilmente arruina
# este despliegue. App Service espera 230 s por omision a que el contenedor
# escuche, y nuestro arranque corre CUATRO migraciones y un collectstatic
# antes de abrir el puerto. En el primer arranque -bases vacias, todas las
# migraciones desde cero- eso se pasa de 230 s y App Service mata el
# contenedor justo antes de que estuviera listo, una y otra vez, con un error
# que solo dice "container didn't respond to HTTP pings".
$ajustes = @(
    "DJANGO_SETTINGS_MODULE=config.settings.prod",
    "ENVIRONMENT=production",
    "WEBSITES_PORT=8080",
    "WEBSITES_CONTAINER_START_TIME_LIMIT=1800",
    "DB_HOST=$PG_HOST",
    "DB_PORT=5432",
    "DB_USER=$PG_USUARIO",
    "DB_SSLMODE=require",
    "TOPUP_PROVIDER=linntae",
    "LINNTAE_ENABLED=true",
    "LINNTAE_ENV=prod",
    "LINNTAE_BASE_URL=https://api.linn.mx/api/v1/",
    "LINNTAE_TYPE_BALANCE=1",
    "ALLOW_REAL_PROVIDER_TRANSACTIONS=true",
    "CASH_PAYMENT_ENABLED=True",
    "REDIS_URL=redis://127.0.0.1:6379/0",
    "CELERY_BROKER_URL=redis://127.0.0.1:6379/1",
    "CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2",
    "EVENT_STREAM_URL=redis://127.0.0.1:6379/3",
    "CORE_SERVICE_URL=http://127.0.0.1:8001",
    "PAYMENTS_SERVICE_URL=http://127.0.0.1:8002",
    "TOPUPS_SERVICE_URL=http://127.0.0.1:8003",
    "BILLPAY_SERVICE_URL=http://127.0.0.1:8004",
    "DJANGO_ALLOWED_HOSTS=$HOST_APP",
    "CSRF_TRUSTED_ORIGINS=$URL_APP",
    "DB_PASS=@Microsoft.KeyVault(SecretUri=$VAULT/DB-PASS/)",
    "DJANGO_SECRET_KEY=@Microsoft.KeyVault(SecretUri=$VAULT/DJANGO-SECRET-KEY/)",
    "SERVICE_S2S_SECRET=@Microsoft.KeyVault(SecretUri=$VAULT/SERVICE-S2S-SECRET/)",
    "LINNTAE_USERNAME=@Microsoft.KeyVault(SecretUri=$VAULT/LINNTAE-USERNAME/)",
    "LINNTAE_PASSWORD=@Microsoft.KeyVault(SecretUri=$VAULT/LINNTAE-PASSWORD/)"
)
Az webapp config appsettings set --name $APP --resource-group $RG `
    --settings @ajustes --output none

# HTTPS obligatorio y SSH habilitado. El SSH es lo que permite entrar al
# contenedor a crear el usuario administrador y a sincronizar el catalogo;
# sin el habria que inventar un endpoint para eso, y un endpoint que crea
# superusuarios es exactamente lo que no debe existir.
Az webapp update --name $APP --resource-group $RG --https-only true --output none
Az webapp config set --name $APP --resource-group $RG --always-on true --output none

Registrar "Reiniciando..."
Az webapp restart --name $APP --resource-group $RG --output none

Registrar ""
Registrar "=============================================="
Registrar "URL        : $URL_APP"
Registrar "PostgreSQL : $PG_HOST"
Registrar "Registro   : $ACR.azurecr.io"
Registrar "Key Vault  : $KV"
Registrar "=============================================="
Registrar ""
Registrar "El primer arranque corre las migraciones de los cuatro servicios."
Registrar "Tarda varios minutos. Para seguirlo:"
Registrar "  az webapp log tail --name $APP --resource-group $RG"
Registrar ""
Registrar "Falta, y NO lo hace este script:"
Registrar "  - crear el usuario administrador"
Registrar "  - sincronizar el catalogo de Linntae"
Registrar "Los dos se hacen por SSH y ninguno gasta saldo."
Registrar ""
Registrar "NINGUNA recarga se ejecuto. Saldo de Linntae gastado: 0.00"
