"""Punto de entrada ASGI del Core Platform.

Se deja preparado aunque hoy se despliega con Gunicorn/WSGI: cuando se
agreguen notificaciones en tiempo real al dashboard (por ejemplo, el estado
de una orden actualizandose sin recargar) sera el punto de entrada.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_asgi_application()
