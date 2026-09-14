"""Configuracion del microservicio de Recargas Telefonicas.

API pura, sin interfaz de usuario. Solo acepta peticiones firmadas del Core.
"""

from __future__ import annotations

from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()

SERVICE_NAME = "topups"
SERVICE_VERSION = env.str("SERVICE_VERSION", default="0.1.0")

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

SECRET_KEY = env.str("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "drf_spectacular",
    "apps.catalog",
    # Catalogo COMERCIAL. Va separado de apps.catalog a proposito: uno es lo
    # que el proveedor dice que vende, el otro lo que SAMY Cloud decide
    # vender. Ver la nota de apps/commercial/models.py.
    "apps.commercial",
    "apps.fulfillment",
    "apps.providers",
    "apps.outbox",
    "apps.api",
]

MIDDLEWARE = [
    "samy_common.observability.middleware.CorrelationMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.api.middleware.ServiceAuthMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {"context_processors": []},
    }
]

# Base de datos propia: samy_topups no puede leer las tablas de otro servicio.
DATABASES = {
    "default": env.db_url(
        "TOPUPS_DATABASE_URL",
        default="postgres://samy_topups:topups_dev_password@localhost:5432/samy_topups",
    )
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "samy:topups",
    }
}

CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", default="redis://localhost:6379/2")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TIMEZONE = "America/Mexico_City"

#: Cola propia por servicio. NO es un detalle cosmetico.
#:
#: Los tres microservicios comparten el mismo Redis como broker. Con la cola
#: por defecto de Celery ("celery"), las tareas periodicas de un servicio
#: caian en el worker de otro, que no las tiene registradas y las **descarta**
#: con "Received unregistered task". El efecto observado: ordenes pagadas cuya
#: recarga no se ejecutaba nunca, de forma intermitente y sin error visible al
#: cajero. Cada worker consume unicamente su cola (ver -Q en docker-compose).
CELERY_TASK_DEFAULT_QUEUE = f"samy.{SERVICE_NAME}"

EVENT_STREAM_URL = env.str("EVENT_STREAM_URL", default="redis://localhost:6379/3")
EVENT_STREAM_NAME = "samy:events:topups"
#: Stream del que se consumen los eventos de pago.
PAYMENTS_STREAM_NAME = "samy:events:payments"
EVENT_CONSUMER_GROUP = "topups"

SERVICE_S2S_SECRET = env.str("SERVICE_S2S_SECRET")
SERVICE_S2S_TOLERANCE_SECONDS = env.int("SERVICE_S2S_TOLERANCE_SECONDS", default=300)
SERVICE_ALLOWED_CALLERS = env.list("SERVICE_ALLOWED_CALLERS", default=["core", "payments"])
SERVICE_AUTH_EXEMPT_PREFIXES = ("/health",)

SERVICE_URLS = {
    "core": env.str("CORE_SERVICE_URL", default="http://core:8000"),
    "payments": env.str("PAYMENTS_SERVICE_URL", default="http://payments:8000"),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["apps.api.permissions.IsSignedService"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "apps.api.exceptions.samy_exception_handler",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SAMY Cloud - Microservicio de Recargas",
    "DESCRIPTION": (
        "Catalogo sincronizado desde el proveedor y ejecucion de recargas. "
        "Ninguna denominacion esta codificada: todo viene del catalogo real."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
}

LANGUAGE_CODE = "es-mx"
TIME_ZONE = "America/Mexico_City"
USE_I18N = False
USE_TZ = True

LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=True)

DEFAULT_CURRENCY = "MXN"

OUTBOX_MAX_ATTEMPTS = env.int("OUTBOX_MAX_ATTEMPTS", default=8)
OUTBOX_BASE_BACKOFF_SECONDS = env.int("OUTBOX_BASE_BACKOFF_SECONDS", default=5)

# ---------------------------------------------------------------------------
# Proveedores de recargas
# ---------------------------------------------------------------------------
# Reloadly: sandbox de auto-servicio, se obtienen llaves el mismo dia.
# Taecel: produccion en Mexico, requiere contrato. Ver docs/api-integrations.md.
TOPUP_PROVIDER = env.str("TOPUP_PROVIDER", default="reloadly")

RELOADLY_MODE = env.str("RELOADLY_MODE", default="SANDBOX")
RELOADLY_CLIENT_ID = env.str("RELOADLY_CLIENT_ID", default="")
RELOADLY_CLIENT_SECRET = env.str("RELOADLY_CLIENT_SECRET", default="")

# TAECEL: cuenta REGISTRADA y activa (13/09/2026), acceso API en tramite.
# Ver docs/readiness-produccion.md y services/topups/apps/providers/taecel.py.
#
# Dos nombres para el ambiente, y eso es un riesgo: TAECEL_ENV es el canonico
# y TAECEL_MODE queda como alias historico. Si ambos estan puestos y NO
# coinciden, el arranque falla en vez de elegir uno. Un servicio que arranca
# "adivinando" si apunta a sandbox o a produccion es como se acaba mandando
# recargas reales creyendo que son de prueba.
_TAECEL_ENV = env.str("TAECEL_ENV", default="").strip().upper()
_TAECEL_MODE_ALIAS = env.str("TAECEL_MODE", default="").strip().upper()

if _TAECEL_ENV and _TAECEL_MODE_ALIAS and _TAECEL_ENV != _TAECEL_MODE_ALIAS:
    raise ImproperlyConfigured(
        f"TAECEL_ENV={_TAECEL_ENV} y TAECEL_MODE={_TAECEL_MODE_ALIAS} se "
        "contradicen. Deja solo TAECEL_ENV (es el nombre canonico) para que no "
        "haya duda de contra que ambiente opera TAECEL."
    )

#: SANDBOX ante ausencia o ante cualquier valor no reconocido. Fail-closed.
TAECEL_ENV = _TAECEL_ENV or _TAECEL_MODE_ALIAS or "SANDBOX"
#: Alias que sigue leyendo el registro. Mismo valor, siempre.
TAECEL_MODE = TAECEL_ENV

#: URL del web service. **Sin valor por omision.** TAECEL no la publica: la
#: entrega junto con las credenciales. Vacia => el adaptador no opera.
TAECEL_BASE_URL = env.str("TAECEL_BASE_URL", default="")
TAECEL_KEY = env.str("TAECEL_KEY", default="")
TAECEL_NIP = env.str("TAECEL_NIP", default="")

#: Firma humana: alguien leyo el manual real de TAECEL y confirmo que las
#: rutas y los campos del adaptador coinciden. Mientras sea False, TAECEL
#: reporta PENDING_CONTRACT aunque haya credenciales completas.
TAECEL_CONTRACT_VERIFIED = env.bool("TAECEL_CONTRACT_VERIFIED", default=False)

#: Rutas. Las dos primeras traen un valor por omision SIN CONFIRMAR; las dos
#: ultimas no traen ninguno porque no se conoce su nombre.
TAECEL_PATH_REQUEST_TXN = env.str("TAECEL_PATH_REQUEST_TXN", default="RequestTXN")
TAECEL_PATH_STATUS_TXN = env.str("TAECEL_PATH_STATUS_TXN", default="StatusTXN")
TAECEL_PATH_BALANCE = env.str("TAECEL_PATH_BALANCE", default="")
TAECEL_PATH_PRODUCTS = env.str("TAECEL_PATH_PRODUCTS", default="")

# ---------------------------------------------------------------------------
# LINNTAE
# ---------------------------------------------------------------------------
# Proveedor mexicano de tiempo aire y pago de servicios, con API REST
# documentada (OpenAPI 2.0.0). ESTADO: DEMO. Ver docs/providers/linntae.md.
#
# Cuatro condiciones independientes para que pueda operar, y ninguna implica
# las otras:
#
#   1. LINNTAE_ENV coherente con LINNTAE_BASE_URL (guarda de host).
#   2. Credenciales completas.
#   3. LINNTAE_TYPE_BALANCE confirmado con Linntae.
#   4. ALLOW_REAL_PROVIDER_TRANSACTIONS=true.
#
# La cuarta esta separada de las otras tres a proposito: tener el sandbox bien
# configurado no es permiso para mover saldo.
LINNTAE_ENABLED = env.bool("LINNTAE_ENABLED", default=False)

#: ``demo`` o ``production``. Un valor no reconocido detiene el arranque en
#: vez de convertirse en sandbox silenciosamente: un error de dedo aqui es la
#: diferencia entre una recarga de prueba y una real, y prefiero que se vea al
#: levantar el servicio que al vender.
#:
#: La lista de sinonimos es corta y deliberada, igual que la de
#: ``samy_common.providers.environment._SINONIMOS``. Acepta ``prod`` porque esa
#: otra tabla ya lo acepta para ``ENVIRONMENT``, y dos variables que describen
#: el mismo concepto con vocabularios distintos son una trampa: escribir
#: ``prod`` en las dos deberia funcionar en las dos, o fallar en las dos. Lo
#: que NO se acepta es nada fuera de la tabla: ``pro`` o ``produccion`` siguen
#: deteniendo el arranque, porque el modo por omision no puede ser el caro.
_LINNTAE_ENV_SINONIMOS = {
    "demo": "demo",
    "sandbox": "demo",
    "production": "production",
    "prod": "production",
}
_LINNTAE_ENV_CRUDO = env.str("LINNTAE_ENV", default="demo").strip().lower()
if _LINNTAE_ENV_CRUDO not in _LINNTAE_ENV_SINONIMOS:
    raise ImproperlyConfigured(
        f"LINNTAE_ENV='{_LINNTAE_ENV_CRUDO}' no se reconoce. Los unicos "
        "valores validos son: " + ", ".join(sorted(_LINNTAE_ENV_SINONIMOS))
    )
LINNTAE_ENV = _LINNTAE_ENV_SINONIMOS[_LINNTAE_ENV_CRUDO]

#: Modo del proveedor derivado del ambiente de Linntae. Existe porque
#: samy_common.providers.environment razona en SANDBOX/PRODUCTION y porque
#: apps.commercial.services.ambiente_actual() lee ``{SLUG}_MODE``. Derivarlo
#: aqui, en un solo lugar, evita tener dos variables que puedan contradecirse.
LINNTAE_MODE = "PRODUCTION" if LINNTAE_ENV == "production" else "SANDBOX"

#: URL base. Por omision la de DEMO, que es publica y esta en su
#: especificacion. Apuntar a produccion exige cambiarla Y poner
#: LINNTAE_ENV=production: la guarda de host rechaza cualquier otra
#: combinacion.
LINNTAE_BASE_URL = env.str("LINNTAE_BASE_URL", default="https://apidemo.linn.mx/api/v1/")

#: Credenciales. Sin valor por omision y jamas en el repositorio.
LINNTAE_USERNAME = env.str("LINNTAE_USERNAME", default="")
LINNTAE_PASSWORD = env.str("LINNTAE_PASSWORD", default="")

#: ``typeBalance`` de las compras. **Vacio por omision, y eso bloquea la
#: venta.** Linntae lo exige como entero obligatorio pero no publica su
#: enumeracion; que 1 sea "SALDO PLATAFORMA" es una inferencia de sus
#: ejemplos, no un dato confirmado. Se pone cuando Linntae lo confirme.
_LINNTAE_TYPE_BALANCE = env.str("LINNTAE_TYPE_BALANCE", default="").strip()
LINNTAE_TYPE_BALANCE = int(_LINNTAE_TYPE_BALANCE) if _LINNTAE_TYPE_BALANCE else None

#: ``extraComision``: lo que Linntae suma al cobro del cliente en SU punto de
#: venta. En SAMY el cobro al cliente lo calcula el motor de precios de SAMY,
#: asi que aqui va CERO. Un valor distinto cambia lo que se carga a la bolsa
#: de plataforma de una forma que la especificacion no detalla, y eso no se
#: activa sin haberlo medido en DEMO.
LINNTAE_EXTRA_COMISION = env.int("LINNTAE_EXTRA_COMISION", default=0)

#: Vida que le damos al token en cache. Linntae NO publica la suya, asi que
#: este numero es nuestro. Corto a proposito.
LINNTAE_TOKEN_TTL_SECONDS = env.int("LINNTAE_TOKEN_TTL_SECONDS", default=600)

LINNTAE_CONNECT_TIMEOUT = env.float("LINNTAE_CONNECT_TIMEOUT", default=5.0)
LINNTAE_READ_TIMEOUT = env.float("LINNTAE_READ_TIMEOUT", default=30.0)
LINNTAE_REINTENTOS_LECTURA = env.int("LINNTAE_REINTENTOS_LECTURA", default=2)

#: COMO aplica Linntae su comision. Ver samy_common.pricing.MecanismoComision.
#:
#: Por omision SIN_DETERMINAR, y eso es lo correcto hoy: su esquema se llama
#: "COMISION SOBRE VENTA" y su consulta de saldo devuelve ``plataforma`` y
#: ``comision`` como bolsas separadas, lo que APUNTA a que el saldo se
#: descuenta al valor facial y la comision se abona aparte. Apuntar no es
#: saber. Con SIN_DETERMINAR el motor de precios reporta margen desconocido en
#: vez de afirmar un numero que nadie ha comprobado.
#:
#: Se resuelve midiendo: saldo antes y despues de una recarga DEMO.
LINNTAE_COMMISSION_MECHANISM = env.str(
    "LINNTAE_COMMISSION_MECHANISM", default="SIN_DETERMINAR"
).strip().upper()

# ---------------------------------------------------------------------------
# Interruptor final de operaciones reales
# ---------------------------------------------------------------------------
# Vale para cualquier proveedor, no solo Linntae. Es independiente de DEBUG y
# de ENVIRONMENT a proposito: DEBUG contesta "estoy depurando" y ENVIRONMENT
# contesta "en que ambiente corro", y ninguna de las dos preguntas es "tengo
# autorizacion para gastar dinero".
ALLOW_REAL_PROVIDER_TRANSACTIONS = env.bool(
    "ALLOW_REAL_PROVIDER_TRANSACTIONS", default=False
)

# ---------------------------------------------------------------------------
# Guarda de saldo del proveedor
# ---------------------------------------------------------------------------
# Se comprueba en create_fulfillment, ANTES de que exista la orden. Ver
# apps/fulfillment/saldo.py y samy_common/saldo.py.

#: Segundos que se cachea el saldo del proveedor. Corto a proposito: Reloadly
#: suspende cuentas por exceso de llamadas, pero un saldo viejo deja pasar
#: ventas con fondos que ya no existen. Segundos, no minutos.
TOPUP_SALDO_CACHE_SECONDS = env.int("TOPUP_SALDO_CACHE_SECONDS", default=30)

#: Proveedores a los que se les mide el saldo ANTES y DESPUES de cada recarga.
#:
#: La medicion es lo que permite deducir como aplica su comision un proveedor
#: nuevo (ver el campo ``economia`` de TopupFulfillment). Cuesta dos llamadas
#: por recarga, asi que se decide por lista y no por bandera global: Reloadly
#: suspende cuentas por exceso de llamadas, y ahi la medicion no hace falta
#: porque su comision ya se conoce.
#:
#: Se apaga sacando el slug de la lista, cuando el mecanismo ya este medido.
TOPUP_MEDIR_SALDO_PROVIDERS = env.list(
    "TOPUP_MEDIR_SALDO_PROVIDERS", default=["linntae"]
)

#: Colchon de saldo que no se vende, en centavos.
#:
#: Vender hasta dejar el saldo exactamente en cero hace que la venta
#: siguiente falle a mitad de camino con la orden ya pagada. Esta reserva
#: absorbe tambien la ventana del cache.
#:
#: Por omision CERO: el valor correcto depende del volumen del negocio y es
#: una decision de Sizu, no un numero que el codigo deba inventar.
TOPUP_RESERVA_SALDO_CENTS = env.int("TOPUP_RESERVA_SALDO_CENTS", default=0)

#: Cada cuanto se refresca el catalogo. Un catalogo viejo puede ofrecer
#: paquetes que el operador ya retiro.
CATALOG_SYNC_HOURS = env.int("CATALOG_SYNC_HOURS", default=6)
CATALOG_STALE_HOURS = env.int("CATALOG_STALE_HOURS", default=24)
