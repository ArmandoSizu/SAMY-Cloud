from django.urls import path

from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
]
