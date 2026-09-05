"""Configuracion de DESARROLLO. Nunca se usa en un entorno desplegado.

Los valores inseguros viven aqui y solo aqui, de modo que produccion no pueda
heredarlos por descuido.
"""

from config.settings.base import *  # noqa: F403
from config.settings.base import INSTALLED_APPS, MIDDLEWARE, env

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal"]

# En desarrollo el navegador entra por http://localhost, sin TLS.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost"],
)

# Logs legibles en consola en vez de JSON.
LOG_JSON = False
LOG_LEVEL = "DEBUG"

# El correo de recuperacion de contrasena se imprime en consola.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# El bloqueo por intentos fallidos estorba al desarrollar; sigue activo pero
# con un limite alto para no bloquearse uno mismo probando el login.
AXES_FAILURE_LIMIT = 20

INSTALLED_APPS = INSTALLED_APPS + ["django_extensions"] if env.bool(
    "USE_DJANGO_EXTENSIONS", default=False
) else INSTALLED_APPS
