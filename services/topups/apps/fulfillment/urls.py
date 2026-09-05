from django.urls import path

from apps.fulfillment import views

app_name = "fulfillment"

urlpatterns = [
    path("", views.create_topup, name="create"),
    path("<uuid:fulfillment_id>/", views.topup_detail, name="detail"),
]
