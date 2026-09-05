"""Configuracion base del Core Platform de SAMY Cloud.

Se divide en base / dev / prod porque una sola configuracion con ``if DEBUG``
esparcido termina, tarde o temprano, desplegando en produccion con una opcion
de desarrollo activa. Aqui produccion nunca hereda un valor inseguro por
descuido: los valores peligrosos solo existen en ``dev.py``.

Nada sensible se lee de codigo fuente: todo viene de variables de entorno.
"""

from __future__ import annotations

from pathlib import Path

import environ

# core/config/settings/base.py -> core/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# ---------------------------------------------------------------------------
# Identidad del servicio
# ---------------------------------------------------------------------------
SERVICE_NAME = "core"
SERVICE_VERSION = env.str("SERVICE_VERSION", default="0.1.0")

# ---------------------------------------------------------------------------
# Seguridad basica
# ---------------------------------------------------------------------------
# Sin valor por defecto a proposito: si falta, el servicio no arranca.
# Un SECRET_KEY con default es la via mas comun de desplegar con la llave
# de ejemplo publicada en el repositorio.
SECRET_KEY = env.str("DJANGO_SECRET_KEY")

DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])

# Ruta del admin de Django. Se configura por entorno para que en produccion no
# viva en /admin/, que es el primer sitio donde apuntan los bots. No es una
# medida de seguridad por si sola (la autenticacion lo es), pero elimina
# practicamente todo el ruido de fuerza bruta automatizada en los logs.
DJANGO_ADMIN_PATH = env.str("DJANGO_ADMIN_PATH", default="admin-samy/")

# ---------------------------------------------------------------------------
# Aplicaciones
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
    "django_htmx",
    "axes",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.tenancy",
    "apps.audit",
    "apps.dashboard",
    "apps.gateway",
    "apps.platform_admin",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# El orden importa. CorrelationMiddleware va primero para que TODO lo demas,
# incluidos los errores de seguridad, quede correlacionado en los logs.
MIDDLEWARE = [
    "samy_common.observability.middleware.CorrelationMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "apps.tenancy.middleware.CurrentStoreMiddleware",
    # django-axes va al final para ver la peticion ya autenticada.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.tenancy.context_processors.current_store",
                "apps.dashboard.context_processors.branding",
            ],
        },
    }
]

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
# PostgreSQL tambien en desarrollo: SQLite no tiene los tipos, constraints ni
# comportamiento transaccional de Postgres, y las diferencias aparecen justo
# en produccion. Ver docs/database.md.
DATABASES = {
    "default": env.db_url(
        "CORE_DATABASE_URL",
        default="postgres://samy:samy@localhost:5432/samy_core",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Autenticacion
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # AxesStandaloneBackend debe ir primero para bloquear por intentos fallidos.
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# Argon2 primero: es el ganador del Password Hashing Competition y la
# recomendacion actual de OWASP. PBKDF2 se mantiene para poder verificar
# hashes antiguos si se migra desde otro sistema.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "accounts:login"

# ---------------------------------------------------------------------------
# Bloqueo por intentos fallidos (django-axes)
# ---------------------------------------------------------------------------
AXES_FAILURE_LIMIT = env.int("AXES_FAILURE_LIMIT", default=5)
AXES_COOLOFF_TIME = env.int("AXES_COOLOFF_MINUTES", default=15) / 60
AXES_LOCKOUT_PARAMETERS = ["ip_address", "username"]
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ADMIN = True
AXES_VERBOSE = True

# ---------------------------------------------------------------------------
# Sesiones y cookies
# ---------------------------------------------------------------------------
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_NAME = "samy_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
# 8 horas: un turno de caja. Mas alla de eso se vuelve a autenticar.
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=8 * 60 * 60)

CSRF_COOKIE_NAME = "samy_csrf"
CSRF_COOKIE_HTTPONLY = False  # HTMX necesita leerla para enviar el token.
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_USE_SESSIONS = False
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# ---------------------------------------------------------------------------
# Cache y Redis
# ---------------------------------------------------------------------------
REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "samy:core",
    }
}

# ---------------------------------------------------------------------------
# Internacionalizacion
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "es-mx"
TIME_ZONE = "America/Mexico_City"
USE_I18N = True
USE_TZ = True  # Siempre UTC en base de datos; se presenta en hora local.

# ---------------------------------------------------------------------------
# Archivos estaticos y media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        # Denegar por defecto. Cada vista abre explicitamente lo que expone.
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
        "orders": "60/min",
        "catalog": "120/min",
        "inquiry": "30/min",
    },
    "EXCEPTION_HANDLER": "apps.gateway.exceptions.samy_exception_handler",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 25,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SAMY Cloud - Core Platform API",
    "DESCRIPTION": (
        "API del Core Platform: autenticacion, organizaciones, tiendas, "
        "usuarios y orquestacion hacia los microservicios comerciales."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/v1",
}

# ---------------------------------------------------------------------------
# Microservicios (comunicacion S2S firmada)
# ---------------------------------------------------------------------------
SERVICE_S2S_SECRET = env.str("SERVICE_S2S_SECRET")
SERVICE_URLS = {
    "payments": env.str("PAYMENTS_SERVICE_URL", default="http://payments:8000"),
    "topups": env.str("TOPUPS_SERVICE_URL", default="http://topups:8000"),
    "billpay": env.str("BILLPAY_SERVICE_URL", default="http://billpay:8000"),
}
SERVICE_TIMEOUT_CONNECT = env.float("SERVICE_TIMEOUT_CONNECT", default=3.0)
SERVICE_TIMEOUT_READ = env.float("SERVICE_TIMEOUT_READ", default=15.0)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=True)

# ---------------------------------------------------------------------------
# Reglas de negocio configurables
# ---------------------------------------------------------------------------
# Nada de esto se codifica en la logica: son parametros del producto.
DEFAULT_CURRENCY = "MXN"
QR_TOKEN_TTL_SECONDS = env.int("QR_TOKEN_TTL_SECONDS", default=240)  # 4 min
QR_TOKEN_TTL_MIN = 180
QR_TOKEN_TTL_MAX = 300
