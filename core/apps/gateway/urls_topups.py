"""Rutas del flujo de recargas (lado Core / BFF)."""

from django.urls import path

from apps.gateway import views_topups as views

app_name = "topups"

urlpatterns = [
    path("", views.catalog, name="catalog"),
    path("<uuid:operator_id>/numero/", views.phone_step, name="phone"),
    path("<uuid:operator_id>/numero/validar/", views.validate_phone, name="validate_phone"),
    path("confirmar/", views.confirm_step, name="confirm"),
    path("crear/", views.create_order, name="create_order"),
]
