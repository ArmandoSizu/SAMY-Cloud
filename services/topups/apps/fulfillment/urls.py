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
    # Ejecucion sincrona, para que la recarga ocurra dentro de la peticion en
    # que una persona autorizada la confirma y no dependa de un worker que en
    # Cloud Run puede desaparecer. No es una puerta nueva al dinero: pasa por
    # las mismas guardas, y esta ruta va firmada S2S como todo el servicio.
    path("<uuid:fulfillment_id>/ejecutar/", views.ejecutar_recarga, name="execute"),
]
