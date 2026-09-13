"""Configuracion de pruebas del microservicio de recargas."""

from config.settings.base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Las tareas se ejecutan en linea: las pruebas no levantan un worker.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

LOG_JSON = False
LOG_LEVEL = "WARNING"

# Ambiente de PRUEBAS. Es lo que impide que la suite opere contra produccion:
# con este valor, un proveedor en modo PRODUCTION es rechazado por
# ensure_ready() antes de autenticarse, y ninguna prueba puede gastar saldo
# real ni mandar una recarga a un telefono real.
ENVIRONMENT = "test"
