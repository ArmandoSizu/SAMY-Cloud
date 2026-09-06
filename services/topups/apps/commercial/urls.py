from django.urls import path

from apps.commercial import views

app_name = "commercial"

urlpatterns = [
    path("", views.catalogo_comercial, name="catalog"),
    path("admin/", views.catalogo_administracion, name="admin"),
]
