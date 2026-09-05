"""Configuracion de PRODUCCION.

Todo lo que aqui se activa responde a un requisito concreto de seguridad
(ver ``docs/security.md``). Ninguna de estas opciones tiene valor por defecto
permisivo: si falta una variable de entorno critica, el servicio no arranca,
que es preferible a arrancar inseguro.
"""

from __future__ import annotations

from config.settings.base import *  # noqa: F403
from config.settings.base import MIDDLEWARE, env

DEBUG = False

# Sin default: desplegar con ALLOWED_HOSTS vacio o con "*" es un error.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise RuntimeError(
        "DJANGO_ALLOWED_HOSTS debe listar dominios concretos en produccion."
    )

# ---------------------------------------------------------------------------
# HTTPS obligatorio
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = True
# El proxy inverso termina TLS; sin esta cabecera Django creeria que toda
# peticion es HTTP y entraria en un bucle de redirecciones.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS: se empieza con un valor bajo y se sube tras verificar que todo el
# dominio sirve HTTPS. Con preload activado un error deja el dominio
# inaccesible durante meses, asi que preload se habilita explicitamente.
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=3600)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)

# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS")

# ---------------------------------------------------------------------------
# Cabeceras de seguridad
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Politica de permisos: la camara se necesita para el lector de codigos, pero
# solo desde el propio origen. Todo lo demas se niega.
MIDDLEWARE = MIDDLEWARE + ["django_permissions_policy.PermissionsPolicyMiddleware"]
PERMISSIONS_POLICY: dict[str, list[str]] = {
    "camera": ["self"],
    "microphone": [],
    "geolocation": [],
    "payment": [],
    "usb": [],
    "interest-cohort": [],
}

# ---------------------------------------------------------------------------
# Content Security Policy
# ---------------------------------------------------------------------------
# Sin 'unsafe-inline' en scripts: HTMX y Alpine se cargan como archivos
# estaticos propios, no desde CDN ni en linea. Ver docs/security.md.
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": ["'self'", "data:", "blob:"],
        "font-src": ["'self'"],
        "connect-src": ["'self'"],
        # blob: es necesario para el flujo de camara del lector de codigos.
        "media-src": ["'self'", "blob:"],
        "worker-src": ["'self'", "blob:"],
        "frame-ancestors": ["'none'"],
        "form-action": ["'self'"],
        "base-uri": ["'self'"],
        "object-src": ["'none'"],
        "upgrade-insecure-requests": True,
    }
}
MIDDLEWARE = ["csp.middleware.CSPMiddleware"] + MIDDLEWARE

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=120)  # noqa: F405
# TLS obligatorio contra la base de datos gestionada.
DATABASES["default"].setdefault("OPTIONS", {})["sslmode"] = env.str(  # noqa: F405
    "DB_SSLMODE", default="require"
)

# ---------------------------------------------------------------------------
# Correo
# ---------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env.str("EMAIL_HOST")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="no-reply@samycloud.mx")

# ---------------------------------------------------------------------------
# Almacenamiento de archivos compatible con nube (S3 / GCS / R2)
# ---------------------------------------------------------------------------
if env.bool("USE_OBJECT_STORAGE", default=False):
    STORAGES["default"] = {  # noqa: F405
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": env.str("AWS_STORAGE_BUCKET_NAME"),
            "region_name": env.str("AWS_S3_REGION_NAME", default="us-east-1"),
            "endpoint_url": env.str("AWS_S3_ENDPOINT_URL", default=None),
            "default_acl": "private",
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }

# ---------------------------------------------------------------------------
# Observabilidad
# ---------------------------------------------------------------------------
LOG_JSON = True

if sentry_dsn := env.str("SENTRY_DSN", default=""):
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[DjangoIntegration()],
        environment=env.str("ENVIRONMENT", default="production"),
        release=env.str("SERVICE_VERSION", default="0.1.0"),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),
        # Nunca enviar PII a un tercero: los eventos ya van saneados por
        # samy_common, y esto es la garantia adicional.
        send_default_pii=False,
    )
