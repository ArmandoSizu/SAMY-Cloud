"""Formularios de autenticacion con mensajes en espanol.

Nota sobre el mensaje de error: se usa el MISMO texto para "no existe la
cuenta" y "contrasena incorrecta". Distinguirlos permitiria enumerar que
correos estan registrados en la plataforma, que es informacion util para un
atacante y no aporta nada al usuario legitimo.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
)
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from samy_common.phone import PhoneValidationError, normalize_mx_phone

User = get_user_model()


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


# ---------------------------------------------------------------------------
# Registro publico
# ---------------------------------------------------------------------------

class SignupAccountForm(forms.Form):
    """Paso 1: la persona.

    Sobre enumeracion de cuentas: aqui SI se dice que el correo ya esta
    registrado. Es una decision consciente y distinta a la del login.

    En el login callar protege: el atacante no obtiene nada util y el usuario
    legitimo tampoco lo necesita. En el registro, callar obligaria a fingir
    que la cuenta se creo cuando no fue asi, y este producto no muestra exitos
    que no ocurrieron. La alternativa habitual (aceptar y avisar por correo)
    exige un servidor de correo en funcionamiento, que todavia no hay.

    El coste de enumerar se sube por otro lado: limite de intentos por IP en
    la vista. Y el dato que se filtra es acotado, porque el correo del negocio
    suele estar publicado en su propia fachada.
    """

    first_name = forms.CharField(label="Nombre", max_length=80)
    last_name = forms.CharField(label="Apellidos", max_length=80)
    email = forms.EmailField(label="Correo electronico", max_length=254)
    phone = forms.CharField(label="Telefono", max_length=20)
    password1 = forms.CharField(label="Contrasena", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirma la contrasena", widget=forms.PasswordInput)

    def clean_first_name(self) -> str:
        return _nombre_valido(self.cleaned_data["first_name"], "nombre")

    def clean_last_name(self) -> str:
        return _nombre_valido(self.cleaned_data["last_name"], "apellidos")

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email=email).exists():
            raise ValidationError(
                "Ya existe una cuenta con este correo. Inicia sesion o recupera tu contrasena."
            )
        return email

    def clean_phone(self) -> str:
        return _telefono_valido(self.cleaned_data["phone"])

    def clean(self) -> dict:
        datos = super().clean()
        password1 = datos.get("password1")
        password2 = datos.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Las dos contrasenas no coinciden.")
            return datos

        if password1:
            # Se valida contra los validadores configurados en settings
            # (longitud minima, contrasenas comunes, solo numeros) y ademas
            # contra los datos que la persona acaba de escribir: usar tu
            # propio correo como contrasena es de los errores mas frecuentes.
            usuario_tentativo = User(
                email=datos.get("email", ""),
                first_name=datos.get("first_name", ""),
                last_name=datos.get("last_name", ""),
            )
            try:
                validate_password(password1, user=usuario_tentativo)
            except ValidationError as exc:
                self.add_error("password1", exc)

        return datos


class SignupBusinessForm(forms.Form):
    """Paso 2: el negocio.

    No se piden RFC, razon social ni domicilio fiscal. Solo hacen falta para
    facturar, y pedirlos aqui alarga el alta y hace que se abandone. Se
    completan despues, cuando el negocio los necesite.
    """

    store_name = forms.CharField(label="Nombre comercial de la tienda", max_length=150)
    organization_name = forms.CharField(label="Nombre de la organizacion", max_length=150)
    store_phone = forms.CharField(label="Telefono del negocio", max_length=20, required=False)
    city = forms.CharField(label="Ciudad", max_length=100)
    state = forms.CharField(label="Estado", max_length=100)
    postal_code = forms.CharField(label="Codigo postal", max_length=5)

    def clean_store_name(self) -> str:
        return _texto_con_fondo(self.cleaned_data["store_name"], "nombre de la tienda")

    def clean_organization_name(self) -> str:
        return _texto_con_fondo(
            self.cleaned_data["organization_name"], "nombre de la organizacion"
        )

    def clean_store_phone(self) -> str:
        valor = (self.cleaned_data.get("store_phone") or "").strip()
        return _telefono_valido(valor) if valor else ""

    def clean_postal_code(self) -> str:
        codigo = self.cleaned_data["postal_code"].strip()
        if not codigo.isdigit() or len(codigo) != 5:
            raise ValidationError("El codigo postal mexicano tiene 5 digitos.")
        return codigo


class SamyPasswordChangeForm(PasswordChangeForm):
    """Cambio de contrasena desde dentro de la sesion.

    Se usa sobre todo para el acceso temporal que entrega el dueno a un
    cajero: el middleware obliga a pasar por aqui antes de dejarle operar.
    """

    error_messages = {
        **PasswordChangeForm.error_messages,
        "password_mismatch": "Las dos contrasenas no coinciden.",
        "password_incorrect": "La contrasena actual no es correcta.",
    }
    old_password = forms.CharField(
        label="Contrasena actual",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    new_password1 = forms.CharField(
        label="Nueva contrasena",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Minimo 12 caracteres. Evita datos personales y secuencias obvias.",
    )
    new_password2 = forms.CharField(
        label="Confirma la contrasena",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )


# ---------------------------------------------------------------------------
# Validaciones compartidas
# ---------------------------------------------------------------------------

def _telefono_valido(raw: str) -> str:
    """Normaliza a 10 digitos usando el validador del plan de numeracion.

    Se guarda normalizado y no como lo escribio la persona para que dos
    formas del mismo numero ("55 1234 5678" y "(55) 1234-5678") no parezcan
    telefonos distintos al buscarlos.
    """
    try:
        return normalize_mx_phone(raw).national
    except PhoneValidationError as exc:
        raise ValidationError(str(exc)) from exc


def _nombre_valido(raw: str, campo: str) -> str:
    valor = " ".join(raw.split())
    if len(valor) < 2:
        raise ValidationError(f"Escribe tu {campo} completo.")
    if any(caracter.isdigit() for caracter in valor):
        raise ValidationError(f"El {campo} no lleva numeros.")
    return valor


def _texto_con_fondo(raw: str, campo: str) -> str:
    valor = " ".join(raw.split())
    if len(valor) < 3:
        raise ValidationError(f"El {campo} debe tener al menos 3 caracteres.")
    return valor
