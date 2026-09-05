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

# --- Archivos estaticos en desarrollo ----------------------------------------
# En produccion se usa el almacenamiento con manifiesto: cada archivo lleva un
# hash en el nombre, lo que permite cachearlo para siempre. Ese almacenamiento
# exige que TODO archivo referenciado con {% static %} exista ya en el
# manifiesto, asi que en desarrollo cualquier archivo nuevo revienta con un 500
# hasta ejecutar collectstatic.
#
# Al desarrollar eso solo estorba: aqui se sirve el archivo tal cual, sin hash
# y sin manifiesto. La compresion y el cacheado agresivo siguen activos en
# produccion, que es donde importan.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
