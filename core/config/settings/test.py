"""Configuracion para la suite de pruebas."""

from config.settings.base import *  # noqa: F403
from config.settings.base import BASE_DIR  # noqa: F401

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Hasher rapido: Argon2 esta calibrado para ser lento a proposito, lo que
# multiplica por diez la duracion de la suite sin aportar nada a las pruebas.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# El bloqueo por intentos fallidos rompe las pruebas de autenticacion.
AXES_ENABLED = False

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"

LOG_JSON = False
LOG_LEVEL = "WARNING"

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
