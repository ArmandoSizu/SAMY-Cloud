from django.urls import path

from apps.fulfillment import views

app_name = "fulfillment"

urlpatterns = [
    path("", views.create_topup, name="create"),
    path("<uuid:fulfillment_id>/", views.topup_detail, name="detail"),
    # Cierra el circulo entre los dos servicios: la orden ya sabe a que
    # recarga corresponde, y esto hace que la recarga sepa su orden. Sin el
    # enlace, execute_topup no puede verificar el pago y no ejecuta.
    path("<uuid:fulfillment_id>/orden/", views.link_order, name="link_order"),
]
