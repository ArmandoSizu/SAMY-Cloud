from django.urls import path

from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    # Diagnostico del lector de codigos. No cobra ni consulta nada: solo
    # comprueba que la camara del equipo lee un codigo de barras.
    path("herramientas/lector/", views.scanner_check, name="scanner_check"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
]
