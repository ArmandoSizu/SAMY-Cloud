"""Formularios de autenticacion con mensajes en espanol.

Nota sobre el mensaje de error: se usa el MISMO texto para "no existe la
cuenta" y "contrasena incorrecta". Distinguirlos permitiria enumerar que
correos estan registrados en la plataforma, que es informacion util para un
atacante y no aporta nada al usuario legitimo.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    SetPasswordForm,
)


class SamyAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Correo o contrasena incorrectos.",
        "inactive": "Esta cuenta esta desactivada. Contacta al administrador.",
    }

    username = forms.EmailField(
        label="Correo electronico",
        widget=forms.EmailInput(attrs={"autocomplete": "username", "inputmode": "email"}),
    )

    def clean_username(self) -> str:
        # El correo se guarda en minusculas; normalizamos para que el login no
        # dependa de como lo escribio el usuario.
        return self.cleaned_data["username"].strip().lower()


class SamyPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        label="Correo electronico",
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"}),
    )

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().lower()


class SamySetPasswordForm(SetPasswordForm):
    error_messages = {
        "password_mismatch": "Las dos contrasenas no coinciden.",
    }
    new_password1 = forms.CharField(
        label="Nueva contrasena",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Minimo 12 caracteres. Evita datos personales y secuencias obvias.",
    )
    new_password2 = forms.CharField(
        label="Confirma la contrasena",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
