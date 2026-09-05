"""Rutas del flujo de pago de servicios (lado Core / BFF)."""

from django.urls import path

from apps.gateway import views_billpay as views

app_name = "billpay"

urlpatterns = [
    path("", views.catalog, name="catalog"),
    path("<uuid:biller_id>/referencia/", views.reference_step, name="reference"),
    path("<uuid:biller_id>/consultar/", views.inquire, name="inquire"),
    path("crear/", views.create_order, name="create_order"),
]
