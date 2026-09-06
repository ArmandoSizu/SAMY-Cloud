"""Configuracion del microservicio de Pagos y Comisiones.

Este servicio no tiene interfaz de usuario: es una API pura que solo acepta
peticiones firmadas del Core y webhooks verificados de los proveedores. Por
eso no lleva plantillas, ni sesiones, ni el admin de Django.

Menos superficie significa menos que asegurar: sin sesiones no hay secuestro
de sesion, sin formularios HTML no hay CSRF que gestionar, y sin admin no hay
un panel expuesto que proteger.
"""

from __future__ import annotations

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

SERVICE_NAME = "payments"
SERVICE_VERSION = env.str("SERVICE_VERSION", default="0.1.0")

SECRET_KEY = env.str("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    # Sin django.contrib.admin, sessions, messages ni staticfiles: este
    # servicio no sirve HTML ni tiene usuarios que inicien sesion.
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "drf_spectacular",
    "apps.orders",
    "apps.payments",
    "apps.commissions",
    "apps.providers",
    "apps.outbox",
    "apps.webhooks",
    "apps.api",
]

MIDDLEWARE = [
    "samy_common.observability.middleware.CorrelationMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    # Autenticacion S2S por firma HMAC. Va despues de Common para que las
    # peticiones OPTIONS y los health checks no requieran firma.
    "apps.api.middleware.ServiceAuthMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Django exige TEMPLATES aunque no se rendericen plantillas (DRF lo usa para
# sus paginas de error). Se deja vacio y sin APP_DIRS.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {"context_processors": []},
    }
]

# ---------------------------------------------------------------------------
# Base de datos PROPIA de este servicio
# ---------------------------------------------------------------------------
# El usuario samy_payments solo tiene privilegios sobre samy_payments. No
# puede leer las tablas del Core ni las de los otros servicios: el aislamiento
# lo impone PostgreSQL, no la buena voluntad del codigo.
DATABASES = {
    "default": env.db_url(
        "PAYMENTS_DATABASE_URL",
        default="postgres://samy_payments:payments_dev_password@localhost:5432/samy_payments",
    )
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Cache, cola y eventos
# ---------------------------------------------------------------------------
REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "samy:payments",
    }
}

CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", default="redis://localhost:6379/2")
CELERY_TASK_ALWAYS_EAGER = False
# Acuse tardio: si un worker muere a mitad de una tarea, la tarea se reentrega
# en vez de perderse. Imprescindible cuando la tarea mueve dinero.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240
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
EVENT_STREAM_NAME = "samy:events:payments"
#: Streams de los servicios que EJECUTAN. Pagos los consume para saber como
#: termino la entrega y cerrar la orden (PROCESSING -> SUCCESS / FAILED).
#: Sin esto la orden se queda en PAID aunque el servicio ya se haya entregado.
FULFILLMENT_STREAM_NAMES = ["samy:events:topups", "samy:events:billpay"]
EVENT_CONSUMER_GROUP = "payments"

# ---------------------------------------------------------------------------
# Autenticacion entre servicios
# ---------------------------------------------------------------------------
SERVICE_S2S_SECRET = env.str("SERVICE_S2S_SECRET")
SERVICE_S2S_TOLERANCE_SECONDS = env.int("SERVICE_S2S_TOLERANCE_SECONDS", default=300)
#: Servicios autorizados a llamar a este. Lista blanca explicita.
SERVICE_ALLOWED_CALLERS = env.list(
    "SERVICE_ALLOWED_CALLERS", default=["core", "topups", "billpay"]
)
#: Rutas que no requieren firma: salud y webhooks (que traen su propia firma
#: del proveedor, verificada en la vista).
SERVICE_AUTH_EXEMPT_PREFIXES = ("/health", "/api/v1/webhooks/")

SERVICE_URLS = {
    "core": env.str("CORE_SERVICE_URL", default="http://core:8000"),
    "topups": env.str("TOPUPS_SERVICE_URL", default="http://topups:8000"),
    "billpay": env.str("BILLPAY_SERVICE_URL", default="http://billpay:8000"),
}

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    # La autenticacion la resuelve el middleware de firma S2S; DRF no gestiona
    # usuarios en este servicio.
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
    "TITLE": "SAMY Cloud - Microservicio de Pagos",
    "DESCRIPTION": (
        "Ordenes, cobros, comisiones, tokens QR y webhooks. "
        "Todas las rutas /api/ requieren firma HMAC del servicio llamante."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
}

# ---------------------------------------------------------------------------
# Internacionalizacion
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "es-mx"
TIME_ZONE = "America/Mexico_City"
USE_I18N = False
USE_TZ = True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=True)

# ---------------------------------------------------------------------------
# Reglas de negocio
# ---------------------------------------------------------------------------
DEFAULT_CURRENCY = "MXN"

#: Vigencia del token/QR de cobro. El requisito pide 3-5 minutos; el rango se
#: fuerza en codigo para que una variable mal puesta no genere un QR eterno.
QR_TOKEN_TTL_SECONDS = env.int("QR_TOKEN_TTL_SECONDS", default=240)
QR_TOKEN_TTL_MIN = 180
QR_TOKEN_TTL_MAX = 300

#: Cuantas veces se reintenta publicar un evento del outbox antes de marcarlo
#: fallido y alertar.
OUTBOX_MAX_ATTEMPTS = env.int("OUTBOX_MAX_ATTEMPTS", default=8)
OUTBOX_BASE_BACKOFF_SECONDS = env.int("OUTBOX_BASE_BACKOFF_SECONDS", default=5)

# ---------------------------------------------------------------------------
# Proveedores de pago
# ---------------------------------------------------------------------------
# Que proveedor atiende cada metodo. Cambiar de pasarela es cambiar estas
# variables, no reescribir codigo.
CARD_PAYMENT_PROVIDER = env.str("CARD_PAYMENT_PROVIDER", default="conekta")
TRANSFER_PAYMENT_PROVIDER = env.str("TRANSFER_PAYMENT_PROVIDER", default="conekta")
QR_PAYMENT_PROVIDER = env.str("QR_PAYMENT_PROVIDER", default="conekta")

# Efectivo: no requiere proveedor externo, opera desde el primer dia.
CASH_PAYMENT_ENABLED = env.bool("CASH_PAYMENT_ENABLED", default=True)

# Conekta. Vacio = NOT_CONFIGURED = el adaptador rechaza operar.
CONEKTA_MODE = env.str("CONEKTA_MODE", default="SANDBOX")
CONEKTA_PRIVATE_KEY = env.str("CONEKTA_PRIVATE_KEY", default="")
CONEKTA_PUBLIC_KEY = env.str("CONEKTA_PUBLIC_KEY", default="")


def _pem(valor: str) -> str:
    """Devuelve un PEM utilizable venga como venga del entorno.

    La llave con la que Conekta firma sus webhooks es un PEM de varias lineas,
    y ``load_pem_public_key`` exige saltos de linea de verdad. Pero el archivo
    .env lo lee el parser de ``env_file`` de Docker Compose, que es estricto:
    una variable por linea. Un PEM pegado tal cual romperia el archivo entero
    y, con el, el arranque de todos los servicios.

    La solucion es guardarlo en una sola linea con ``\\n`` escapados y
    devolverlo aqui a su forma real. Se aceptan ambas formas a proposito: si
    alguien pega el PEM con saltos reales (por ejemplo exportando la variable
    a mano en su shell), tambien funciona, en vez de fallar con un error de
    criptografia imposible de relacionar con la causa.
    """
    limpio = (valor or "").strip().strip('"').strip("'")
    return limpio.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")


CONEKTA_WEBHOOK_PUBLIC_KEY = _pem(env.str("CONEKTA_WEBHOOK_PUBLIC_KEY", default=""))

#: Ventana de tolerancia para webhooks repetidos, en segundos. Un webhook con
#: el mismo id dentro de esta ventana se ignora sin procesar.
WEBHOOK_DEDUPE_WINDOW_SECONDS = env.int("WEBHOOK_DEDUPE_WINDOW_SECONDS", default=86400)
