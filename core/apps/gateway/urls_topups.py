"""Rutas del flujo de recargas (lado Core / BFF)."""

from django.urls import path

from apps.gateway import views_topups as views

app_name = "topups"

urlpatterns = [
    # El catalogo COMERCIAL es ahora la pantalla de recargas. El catalogo
    # tecnico del proveedor sigue existiendo, pero deja de ser la experiencia
    # normal del cajero: sus denominaciones de sandbox ($89.85, $179.70) no
    # son productos que nadie pida en un mostrador mexicano.
    path("", views.commercial_catalog, name="catalog"),
    path("<slug:operator_code>/familias/", views.commercial_families, name="families"),
    path(
        "<slug:operator_code>/<slug:family_code>/productos/",
        views.commercial_products,
        name="products",
    ),
    # Catalogo tecnico. Se conserva para pruebas de extremo a extremo.
    path("tecnico/", views.catalog, name="technical_catalog"),
    path("<uuid:operator_id>/numero/", views.phone_step, name="phone"),
    path("<uuid:operator_id>/numero/validar/", views.validate_phone, name="validate_phone"),
    path("confirmar/", views.confirm_step, name="confirm"),
    path("crear/", views.create_order, name="create_order"),
]
