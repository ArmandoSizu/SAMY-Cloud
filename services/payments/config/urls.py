"""Rutas del microservicio de pagos.

Solo API. No hay HTML, ni admin, ni login: este servicio no lo consume una
persona, lo consumen el Core y los webhooks de los proveedores.
"""

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.api import health

urlpatterns = [
    path("health/", health.liveness, name="health"),
    path("health/ready/", health.readiness, name="health_ready"),

    path("api/v1/orders/", include("apps.orders.urls")),
    path("api/v1/commissions/", include("apps.commissions.urls")),
    path("api/v1/providers/", include("apps.providers.urls")),
    path("api/v1/webhooks/", include("apps.webhooks.urls")),

    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
