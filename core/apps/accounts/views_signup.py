"""Alta publica de un negocio, en dos pantallas.

Por que dos pantallas y no una: son dos temas distintos (quien eres / cual es
tu negocio) y un formulario unico de once campos se abandona. Ademas, si el
correo ya existe, el error aparece en el paso 1 y la persona no ha perdido el
tiempo escribiendo los datos del negocio.

Como viaja el paso 1 hasta el paso 2:

    La contrasena se cifra EN CUANTO se valida el paso 1 y lo que se guarda en
    la sesion es el hash, nunca el texto. La sesion vive en Redis; dejar ahi
    una contrasena en claro, aunque sean dos minutos, es un riesgo que no hace
    falta correr.

Y el rol: no hay ningun campo de rol en ningun formulario. ``register_owner``
crea STORE_OWNER porque es lo unico que sabe crear. No existe combinacion de
parametros que produzca un PLATFORM_ADMIN por esta via.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

import structlog

from apps.accounts import throttling
from apps.accounts.forms import SignupAccountForm, SignupBusinessForm
from apps.accounts.services import register_owner
from apps.audit import services as audit
from apps.audit.models import AuditAction

log = structlog.get_logger("accounts.signup")

#: Clave de sesion donde espera el paso 1 mientras se llena el paso 2.
SESSION_KEY_PENDING = "samy_signup_pending"

#: Cuanto vale un paso 1 sin terminar. Pasado ese tiempo se descarta y se
#: vuelve a empezar: un registro a medias no debe quedarse en la sesion
#: indefinidamente.
PENDING_TTL_SECONDS = 30 * 60


@never_cache
@csrf_protect
@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def signup_account(request: HttpRequest) -> HttpResponse:
    """Paso 1 de 2: datos de la persona."""
    if request.user.is_authenticated:
        return redirect("dashboard:home")

    if request.method == "GET":
        return render(
            request,
            "accounts/signup_account.html",
            {"form": SignupAccountForm(), "paso": 1},
        )

    ip = throttling.client_ip(request)
    if not throttling.check("signup_step", ip, throttling.SIGNUP_STEP_RATE):
        log.warning("signup_step_rate_limited", ip=ip)
        return render(
            request,
            "accounts/signup_account.html",
            {"form": SignupAccountForm(), "paso": 1, "rate_limited": True},
            status=429,
        )

    form = SignupAccountForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "accounts/signup_account.html",
            {"form": form, "paso": 1},
            status=422,
        )

    datos = form.cleaned_data
    request.session[SESSION_KEY_PENDING] = {
        "first_name": datos["first_name"],
        "last_name": datos["last_name"],
        "email": datos["email"],
        "phone": datos["phone"],
        # Cifrada ya. Entre pantalla y pantalla no viaja texto plano.
        "password_hash": make_password(datos["password1"]),
        "started_at": timezone.now().isoformat(),
    }
    return redirect("accounts:signup_business")


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def signup_business(request: HttpRequest) -> HttpResponse:
    """Paso 2 de 2: datos del negocio. Aqui se crea todo."""
    if request.user.is_authenticated:
        return redirect("dashboard:home")

    pendiente = _pending_or_none(request)
    if pendiente is None:
        messages.info(request, "Vuelve a empezar: la sesion de registro caduco.")
        return redirect("accounts:signup")

    if request.method == "GET":
        return render(
            request,
            "accounts/signup_business.html",
            {"form": SignupBusinessForm(), "email": pendiente["email"], "paso": 2},
        )

    ip = throttling.client_ip(request)
    if not throttling.check("signup", ip, throttling.SIGNUP_RATE):
        log.warning("signup_rate_limited", ip=ip)
        return render(
            request,
            "accounts/signup_business.html",
            {
                "form": SignupBusinessForm(),
                "email": pendiente["email"],
                "paso": 2,
                "rate_limited": True,
            },
            status=429,
        )

    form = SignupBusinessForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "accounts/signup_business.html",
            {"form": form, "email": pendiente["email"], "paso": 2},
            status=422,
        )

    negocio = form.cleaned_data

    # Segunda comprobacion del correo, ahora contra la base y justo antes de
    # escribir. Entre el paso 1 y el paso 2 pudo registrarse alguien con ese
    # mismo correo; sin esto, la insercion reventaria con un error feo.
    from django.contrib.auth import get_user_model

    if get_user_model().objects.filter(email=pendiente["email"]).exists():
        request.session.pop(SESSION_KEY_PENDING, None)
        messages.error(
            request,
            "Ese correo se registro mientras completabas el formulario. Inicia sesion.",
        )
        return redirect("accounts:login")

    resultado = register_owner(
        email=pendiente["email"],
        password_hash=pendiente["password_hash"],
        first_name=pendiente["first_name"],
        last_name=pendiente["last_name"],
        phone=pendiente["phone"],
        organization_name=negocio["organization_name"],
        store_name=negocio["store_name"],
        store_phone=negocio.get("store_phone", ""),
        city=negocio["city"],
        state=negocio["state"],
        postal_code=negocio["postal_code"],
    )

    audit.record(
        request,
        AuditAction.EMPLOYEE_ADDED,
        store=resultado.store,
        actor=resultado.user,
        object_type="Membership",
        object_id=str(resultado.membership.id),
        new_state="STORE_OWNER",
        metadata={
            "motivo": "alta_publica",
            "email": resultado.user.email,
            "organizacion": resultado.organization.name,
            "tienda": resultado.store.name,
            "codigo_tienda": resultado.store.code,
        },
    )

    request.session.pop(SESSION_KEY_PENDING, None)
    throttling.reset("signup_step", ip)

    # No se inicia sesion automaticamente.
    #
    # De las dos opciones, esta es la mas segura: una peticion POST de registro
    # nunca devuelve una sesion iniciada, asi que un alta forzada desde otro
    # sitio no puede dejar al visitante dentro de una cuenta ajena en su propio
    # navegador. Ademas obliga a comprobar en el acto que la contrasena quedo
    # como la persona cree, en vez de descubrirlo manana sin poder entrar a su
    # propio negocio.
    request.session["samy_signup_email"] = resultado.user.email
    return redirect("accounts:signup_done")


@never_cache
@require_http_methods(["GET"])
def signup_done(request: HttpRequest) -> HttpResponse:
    """Confirmacion del alta, con el correo ya escrito para entrar."""
    email = request.session.pop("samy_signup_email", "")
    if not email:
        return redirect("accounts:login")
    return render(request, "accounts/signup_done.html", {"email": email})


def _pending_or_none(request: HttpRequest) -> dict[str, Any] | None:
    """Devuelve el paso 1 guardado, o ``None`` si no existe o caduco."""
    pendiente = request.session.get(SESSION_KEY_PENDING)
    if not isinstance(pendiente, dict) or "password_hash" not in pendiente:
        return None

    from django.utils.dateparse import parse_datetime

    empezado = parse_datetime(pendiente.get("started_at", "") or "")
    if empezado is None:
        return None
    if (timezone.now() - empezado).total_seconds() > PENDING_TTL_SECONDS:
        request.session.pop(SESSION_KEY_PENDING, None)
        return None
    return pendiente
