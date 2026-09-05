"""Rutas del microservicio de recargas. Solo API."""

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.api import health

urlpatterns = [
    path("health/", health.liveness, name="health"),
    path("health/ready/", health.readiness, name="health_ready"),

    path("api/v1/catalog/", include("apps.catalog.urls")),
    path("api/v1/topups/", include("apps.fulfillment.urls")),
    path("api/v1/providers/", include("apps.providers.urls")),

    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
