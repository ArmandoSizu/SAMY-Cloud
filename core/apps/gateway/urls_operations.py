"""Rutas de cobro, comprobante e historial."""

from django.urls import path

from apps.gateway import views_operations as views

app_name = "operations"

urlpatterns = [
    path("historial/", views.history, name="history"),
    path("<uuid:order_id>/cobrar/", views.pay, name="pay"),
    path("<uuid:order_id>/cobrar/efectivo/", views.pay_cash, name="pay_cash"),
    # Dos rutas para la tarjeta: la pantalla que carga el tokenizador (GET) y
    # el cobro con el token ya generado (POST). Separarlas permite volver al
    # formulario cuando un cobro falla, porque el token es de un solo uso.
    path("<uuid:order_id>/cobrar/tarjeta/", views.card_form, name="card"),
    path("<uuid:order_id>/cobrar/tarjeta/procesar/", views.pay_card, name="pay_card"),
    path("<uuid:order_id>/comprobante/", views.receipt, name="receipt"),
    path("<uuid:order_id>/estado/", views.order_status, name="status"),
]
