from django.urls import path

from apps.fulfillment import views

app_name = "fulfillment"

urlpatterns = [
    path("", views.create_payment, name="create"),
    path("<uuid:fulfillment_id>/", views.payment_detail, name="detail"),
]
