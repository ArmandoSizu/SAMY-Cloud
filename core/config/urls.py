"""Rutas del Core Platform.

Convencion: las rutas visibles para el usuario van en espanol (``/entrar/``,
``/recargas/``) porque son parte de la interfaz; las rutas de API van en
ingles y versionadas (``/api/v1/...``) porque son un contrato tecnico.

El versionado en la ruta (``/api/v1/``) es deliberado frente a versionar por
cabecera: es visible en los logs, en el navegador y en las trazas, lo que
hace mucho mas facil depurar en produccion.
"""

from __future__ import annotations

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.gateway import health

urlpatterns = [
    # --- Salud y observabilidad (sin autenticacion, por diseno) ---------
    # El balanceador y el orquestador necesitan consultarlas antes de que
    # exista una sesion. No exponen ningun dato de negocio.
    path("health/", health.liveness, name="health"),
    path("health/ready/", health.readiness, name="health_ready"),
    path("health/services/", health.services_health, name="health_services"),

    # --- Autenticacion --------------------------------------------------
    path("", include("apps.accounts.urls")),

    # --- Aplicacion -----------------------------------------------------
    path("", include("apps.dashboard.urls")),
    path("recargas/", include("apps.gateway.urls_topups", namespace="topups")),
    path("servicios/", include("apps.gateway.urls_billpay", namespace="billpay")),
    path("operaciones/", include("apps.gateway.urls_operations", namespace="operations")),

    # --- Panel de plataforma --------------------------------------------
    path("plataforma/", include("apps.platform_admin.urls")),

    # --- API ------------------------------------------------------------
    path("api/v1/", include("apps.gateway.urls_api")),

    # --- Documentacion OpenAPI -------------------------------------------
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),

    # --- Admin de Django (herramienta interna del equipo) ----------------
    # Ruta no adivinable: reduce el ruido de bots que prueban /admin/.
    path(settings.DJANGO_ADMIN_PATH, admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


# Personalizacion del admin
admin.site.site_header = "SAMY Cloud - Administracion interna"
admin.site.site_title = "SAMY Cloud"
admin.site.index_title = "Plataforma"
