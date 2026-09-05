"""Formularios de administracion de empleados.

Lo que NO tiene este formulario, a proposito: un campo de rol.

El dueno da de alta cajeros; es lo unico que puede crear. Si el rol fuera un
campo del formulario, aunque en la plantilla apareciera como un desplegable
con una sola opcion, bastaria con enviar el POST a mano para pedir
PLATFORM_ADMIN. El rol se fija en el servidor, en
``apps.accounts.services.create_cashier``, y no se lee de la peticion.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from apps.tenancy.models import Membership
from samy_common.phone import PhoneValidationError, normalize_mx_phone

User = get_user_model()


class CashierForm(forms.Form):
    """Alta de un cajero en la tienda activa."""

    first_name = forms.CharField(label="Nombre", max_length=80)
    last_name = forms.CharField(label="Apellidos", max_length=80)
    email = forms.EmailField(label="Correo electronico", max_length=254)
    phone = forms.CharField(label="Telefono", max_length=20, required=False)

    def __init__(self, *args, store=None, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)

    def clean_first_name(self) -> str:
        return _nombre(self.cleaned_data["first_name"], "nombre")

    def clean_last_name(self) -> str:
        return _nombre(self.cleaned_data["last_name"], "apellidos")

    def clean_phone(self) -> str:
        valor = (self.cleaned_data.get("phone") or "").strip()
        if not valor:
            return ""
        try:
            return normalize_mx_phone(valor).national
        except PhoneValidationError as exc:
            raise ValidationError(str(exc)) from exc

    def clean_email(self) -> str:
        """Se rechaza solo si esa persona YA trabaja aqui y sigue activa.

        Que el correo exista en la plataforma no es un problema: puede ser
        alguien que ya trabaja en otra tienda, y en ese caso se le anade una
        membresia mas. Lo que no tiene sentido es darlo de alta dos veces en
        la misma tienda.
        """
        email = self.cleaned_data["email"].strip().lower()

        if self.store is None:
            return email

        ya_trabaja = Membership.objects.filter(
            user__email=email, store=self.store, is_active=True
        ).exists()
        if ya_trabaja:
            raise ValidationError("Esa persona ya esta dada de alta en esta tienda.")

        return email


def _nombre(raw: str, campo: str) -> str:
    valor = " ".join(raw.split())
    if len(valor) < 2:
        raise ValidationError(f"Escribe el {campo} completo.")
    if any(caracter.isdigit() for caracter in valor):
        raise ValidationError(f"El {campo} no lleva numeros.")
    return valor
