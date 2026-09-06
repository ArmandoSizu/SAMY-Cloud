"""Configuracion del microservicio de Recargas Telefonicas.

API pura, sin interfaz de usuario. Solo acepta peticiones firmadas del Core.
"""

from __future__ import annotations

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()

SERVICE_NAME = "topups"
SERVICE_VERSION = env.str("SERVICE_VERSION", default="0.1.0")

SECRET_KEY = env.str("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "drf_spectacular",
    "apps.catalog",
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

TAECEL_MODE = env.str("TAECEL_MODE", default="SANDBOX")
TAECEL_KEY = env.str("TAECEL_KEY", default="")
TAECEL_NIP = env.str("TAECEL_NIP", default="")

#: Cada cuanto se refresca el catalogo. Un catalogo viejo puede ofrecer
#: paquetes que el operador ya retiro.
CATALOG_SYNC_HOURS = env.int("CATALOG_SYNC_HOURS", default=6)
CATALOG_STALE_HOURS = env.int("CATALOG_STALE_HOURS", default=24)
