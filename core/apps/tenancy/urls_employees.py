"""Rutas del panel de empleados.

Las tres acciones que cambian algo son POST. Con GET bastaria un enlace o una
imagen incrustada en otra pagina para desactivar a un cajero desde el
navegador del dueno, sin que se enterara.
"""

from django.urls import path

from apps.tenancy import views_employees as views

app_name = "employees"

urlpatterns = [
    path("", views.employee_list, name="list"),
    path("nuevo/", views.employee_new, name="new"),
    path("<uuid:membership_id>/estado/", views.employee_toggle, name="toggle"),
    path("<uuid:membership_id>/restablecer/", views.employee_reset, name="reset"),
]
