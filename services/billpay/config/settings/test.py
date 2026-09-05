"""Configuracion de pruebas del microservicio de pago de servicios."""

from config.settings.base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Las tareas se ejecutan en linea: las pruebas no levantan un worker.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

LOG_JSON = False
LOG_LEVEL = "WARNING"
