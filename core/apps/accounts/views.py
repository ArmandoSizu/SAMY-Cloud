"""Vistas de autenticacion de SAMY Cloud.

Se usan las vistas de ``django.contrib.auth`` como base y solo se sobreescribe
lo necesario: plantilla propia, auditoria y registro de IP. Reimplementar el
login desde cero seria reescribir codigo que Django ya tiene bien resuelto
(rotacion de sesion, tiempos constantes, tokens de recuperacion firmados).

Lo que si se anade:

* Auditoria de cada intento, exitoso o fallido.
* Registro de la IP del ultimo acceso.
* Deteccion de bloqueo por ``django-axes`` para mostrar un mensaje claro.
* Redireccion segura tras el login (se valida el destino ``next``).
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts.forms import (
    SamyAuthenticationForm,
    SamyPasswordChangeForm,
    SamyPasswordResetForm,
    SamySetPasswordForm,
)
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.tenancy.middleware import set_active_store


class LoginView(auth_views.LoginView):
    """Inicio de sesion con auditoria."""

    template_name = "accounts/login.html"
    authentication_form = SamyAuthenticationForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # ``django-axes`` marca la peticion cuando la cuenta esta bloqueada.
        context["locked_out"] = getattr(self.request, "axes_locked_out", False)
        return context

    def form_valid(self, form) -> HttpResponse:
        response = super().form_valid(form)

        user = form.get_user()
        ip = self.request.META.get("REMOTE_ADDR")
        forwarded = self.request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        user.last_login_ip = ip
        user.save(update_fields=["last_login_ip"])

        audit.record_login(self.request, success=True, email=user.email)

        if not user.active_memberships().exists() and not user.is_platform_admin:
            messages.warning(
                self.request,
                "Tu cuenta no tiene ninguna tienda asignada. "
                "Pide al propietario que te agregue como empleado.",
            )
        return response

    def form_invalid(self, form) -> HttpResponse:
        audit.record_login(
            self.request,
            success=False,
            email=form.data.get("username", "")[:150],
        )
        return super().form_invalid(form)

    def get_success_url(self) -> str:
        """Valida el destino para evitar redireccion abierta."""
        redirect_to = self.request.POST.get("next") or self.request.GET.get("next")
        if redirect_to and url_has_allowed_host_and_scheme(
            url=redirect_to,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return redirect_to
        return str(reverse_lazy("dashboard:home"))


class LogoutView(auth_views.LogoutView):
    """Cierre de sesion. Solo por POST, para que no se pueda forzar con un enlace."""

    next_page = reverse_lazy("accounts:login")

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated:
            audit.record(request, AuditAction.LOGOUT)
        return super().post(request, *args, **kwargs)


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/email/password_reset.txt"
    subject_template_name = "accounts/email/password_reset_subject.txt"
    form_class = SamyPasswordResetForm
    success_url = reverse_lazy("accounts:password_reset_done")

    def form_valid(self, form) -> HttpResponse:
        audit.record(
            self.request,
            AuditAction.PASSWORD_RESET_REQUESTED,
            metadata={"email": form.cleaned_data.get("email", "")},
        )
        return super().form_valid(form)


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    form_class = SamySetPasswordForm
    success_url = reverse_lazy("accounts:password_reset_complete")

    def form_valid(self, form) -> HttpResponse:
        response = super().form_valid(form)
        user = form.user
        if user.must_change_password:
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
        audit.record(self.request, AuditAction.PASSWORD_CHANGED, actor=user)
        return response


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


class PasswordChangeView(auth_views.PasswordChangeView):
    """Cambio de contrasena estando dentro.

    Es la salida obligada del acceso temporal que el dueno entrega a un
    cajero: ``ForcePasswordChangeMiddleware`` encierra la sesion aqui hasta
    que la contrasena se cambia. Por eso al terminar se limpia la marca y se
    va directo al panel, sin una pantalla intermedia de "listo" que solo
    anadiria un clic entre el cajero y su primera venta.
    """

    template_name = "accounts/password_change.html"
    form_class = SamyPasswordChangeForm
    success_url = reverse_lazy("dashboard:home")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # La plantilla explica POR QUE se le pide el cambio, que no es lo
        # mismo si entro con una clave temporal que si vino por su cuenta.
        context["es_obligatorio"] = self.request.user.must_change_password
        return context

    def form_valid(self, form) -> HttpResponse:
        response = super().form_valid(form)
        user = form.user
        if user.must_change_password:
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
        audit.record(self.request, AuditAction.PASSWORD_CHANGED, actor=user)
        messages.success(self.request, "Tu contrasena quedo actualizada.")
        return response


@never_cache
@sensitive_post_parameters()
def switch_store(request: HttpRequest, store_id: str) -> HttpResponseRedirect:
    """Cambia la tienda activa.

    Solo POST: cambiar de contexto de datos con un GET permitiria forzarlo
    desde un enlace o una imagen incrustada.
    """
    if request.method != "POST":
        return redirect("dashboard:home")

    previous = str(request.store.id) if request.store else ""
    if set_active_store(request, store_id):
        audit.record(
            request,
            AuditAction.STORE_SWITCHED,
            metadata={"from_store": previous, "to_store": str(store_id)},
        )
        messages.success(request, f"Ahora operas en {request.store.name}.")
    else:
        messages.error(request, "No tienes acceso a esa tienda.")

    return redirect("dashboard:home")
