from django.urls import path

from apps.providers import views

app_name = "providers"

urlpatterns = [
    path("", views.providers_status, name="status"),
]
