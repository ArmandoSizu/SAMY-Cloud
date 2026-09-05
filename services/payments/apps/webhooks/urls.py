"""Rutas de webhooks.

Exentas de la firma S2S interna (ver SERVICE_AUTH_EXEMPT_PREFIXES): traen la
firma criptografica del propio proveedor, que se verifica en la vista.
"""

from django.urls import path

from apps.webhooks import views

app_name = "webhooks"

urlpatterns = [
    path("conekta/", views.conekta, name="conekta"),
]
