"""Configuracion de desarrollo del microservicio de pago de servicios."""

from config.settings.base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]  # solo alcanzable dentro de la red interna de Docker

LOG_JSON = False
LOG_LEVEL = "DEBUG"

SECURE_SSL_REDIRECT = False
