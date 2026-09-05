"""API publica del Core Platform.

Deliberadamente pequeña: el Core sirve HTML al navegador, no una API para
terceros. Lo que se expone aqui es lo minimo para una futura app movil y para
que el panel de plataforma consulte el estado de las integraciones.
"""

from django.urls import path

from apps.gateway import views_api as views

urlpatterns = [
    path("me/", views.me, name="api_me"),
    path("stores/", views.my_stores, name="api_my_stores"),
    path("providers/status/", views.providers_status, name="api_providers_status"),
]
