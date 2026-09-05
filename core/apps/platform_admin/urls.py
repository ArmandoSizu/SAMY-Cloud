from django.urls import path

from apps.platform_admin import views

app_name = "platform_admin"

urlpatterns = [
    path("", views.home, name="home"),
    path("proveedores/", views.providers, name="providers"),
    path("tiendas/", views.stores, name="stores"),
    path("auditoria/", views.audit_log, name="audit"),
]
