"""Registro en el admin de Django.

El admin se usa solo como herramienta interna del equipo de plataforma, nunca
como panel del cliente: el dueno de la tienda tiene su propio panel disenado.
Por eso el acceso queda restringido a ``is_staff``.
"""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email", "full_name", "is_platform_admin", "is_active", "date_joined")
    list_filter = ("is_platform_admin", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)
    readonly_fields = ("date_joined", "last_login", "last_login_ip")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Datos personales", {"fields": ("first_name", "last_name", "phone")}),
        (
            "Permisos",
            {
                "fields": (
                    "is_active",
                    "is_platform_admin",
                    "is_staff",
                    "is_superuser",
                    "must_change_password",
                )
            },
        ),
        ("Actividad", {"fields": ("date_joined", "last_login", "last_login_ip")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "is_platform_admin"),
            },
        ),
    )
