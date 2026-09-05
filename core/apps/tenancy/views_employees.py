"""Empleados de la tienda: alta, baja y restablecimiento de acceso.

Todo lo de aqui exige el permiso ``store.manage_employees``, que tienen el
propietario y el administrador de plataforma, y NO tiene el cajero. Un cajero
que escriba /empleados/ en la barra de direcciones recibe un 403 y el intento
queda en la bitacora.

Aislamiento: cada consulta parte de ``request.store``, la tienda activa que
resuelve el middleware revalidando la membresia en cada peticion. Nunca se
lee una tienda de la URL. Por eso un dueno no puede tocar los empleados de
otro negocio ni cambiando identificadores a mano: la tienda no viene de la
peticion, viene de su propia membresia.
"""

from __future__ import annotations

import uuid

import structlog
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts import throttling
from apps.accounts.services import create_cashier, reset_employee_access
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.tenancy.forms import CashierForm
from apps.tenancy.models import Membership, Role
from apps.tenancy.permissions import require_perm

log = structlog.get_logger("tenancy.employees")

#: Clave de sesion donde viaja la clave temporal recien generada, para
#: mostrarla UNA vez tras la redireccion. No se pasa por la URL: quedaria en
#: el historial del navegador y en los registros del proxy.
SESSION_KEY_CREDENCIAL = "samy_credencial_temporal"


@login_required
@require_perm("store.manage_employees")
@require_GET
def employee_list(request: HttpRequest) -> HttpResponse:
    """Lista de quien trabaja en la tienda activa."""
    if request.store is None:
        return render(request, "employees/no_store.html", status=200)

    empleados = (
        Membership.objects.filter(store=request.store)
        .select_related("user")
        .order_by("role", "user__first_name", "user__email")
    )

    credencial = request.session.pop(SESSION_KEY_CREDENCIAL, None)

    return render(
        request,
        "employees/list.html",
        {
            "empleados": empleados,
            "activos": sum(1 for e in empleados if e.is_active),
            "credencial": credencial,
        },
    )


@login_required
@require_perm("store.manage_employees")
def employee_new(request: HttpRequest) -> HttpResponse:
    """Alta de un cajero.

    El rol NO se lee de la peticion: ``create_cashier`` solo sabe crear
    CASHIER. Aunque el POST traiga ``role=PLATFORM_ADMIN``, se ignora.
    """
    if request.store is None:
        raise Http404("No hay tienda activa.")

    if request.method == "GET":
        return render(request, "employees/new.html", {"form": CashierForm()})

    if not throttling.check(
        "employee_create", str(request.user.id), throttling.EMPLOYEE_CREATE_RATE
    ):
        log.warning("employee_create_rate_limited", user_id=str(request.user.id))
        return render(
            request,
            "employees/new.html",
            {"form": CashierForm(), "rate_limited": True},
            status=429,
        )

    form = CashierForm(request.POST, store=request.store)
    if not form.is_valid():
        return render(request, "employees/new.html", {"form": form}, status=422)

    datos = form.cleaned_data
    membership, clave_temporal = create_cashier(
        store=request.store,
        email=datos["email"],
        first_name=datos["first_name"],
        last_name=datos["last_name"],
        phone=datos.get("phone", ""),
    )

    audit.record(
        request,
        AuditAction.EMPLOYEE_ADDED,
        store=request.store,
        object_type="Membership",
        object_id=str(membership.id),
        new_state=Role.CASHIER,
        metadata={"email": membership.user.email},
    )

    if clave_temporal:
        request.session[SESSION_KEY_CREDENCIAL] = {
            "email": membership.user.email,
            "password": clave_temporal,
            "nombre": membership.user.full_name,
            "motivo": "alta",
        }
        messages.success(request, f"{membership.user.full_name} quedo dado de alta.")
    else:
        # El usuario ya existia en la plataforma: conserva su contrasena.
        messages.success(
            request,
            f"{membership.user.full_name} ya tenia cuenta en SAMY Cloud y se agrego "
            "a esta tienda. Entra con la contrasena que ya usaba.",
        )

    return redirect("employees:list")


@login_required
@require_perm("store.manage_employees")
@require_POST
def employee_toggle(request: HttpRequest, membership_id: uuid.UUID) -> HttpResponse:
    """Activa o desactiva a un empleado.

    Se cambia la MEMBRESIA, no el usuario: la misma persona puede seguir
    trabajando en otra tienda. Desactivar el usuario entero le cerraria una
    puerta que este dueno no tiene por que poder cerrar.

    El efecto es inmediato: ``CurrentStoreMiddleware`` revalida la membresia
    en cada peticion, asi que el cajero deja de operar en su siguiente clic,
    sin esperar a que caduque la sesion.
    """
    membership = _empleado_gestionable(request, membership_id)
    if membership is None:
        return redirect("employees:list")

    membership.is_active = not membership.is_active
    membership.save(update_fields=["is_active", "updated_at"])

    audit.record(
        request,
        AuditAction.EMPLOYEE_ADDED if membership.is_active else AuditAction.EMPLOYEE_REMOVED,
        store=request.store,
        object_type="Membership",
        object_id=str(membership.id),
        new_state="ACTIVO" if membership.is_active else "INACTIVO",
        metadata={"email": membership.user.email},
    )

    estado = "reactivado" if membership.is_active else "desactivado"
    messages.success(request, f"{membership.user.full_name} quedo {estado}.")
    return redirect("employees:list")


@login_required
@require_perm("store.manage_employees")
@require_POST
def employee_reset(request: HttpRequest, membership_id: uuid.UUID) -> HttpResponse:
    """Genera una clave temporal nueva para un empleado.

    Es el caso del mostrador: el cajero olvido su contrasena y no tiene el
    correo a mano. El dueno le entrega una clave nueva y el sistema le obliga
    a cambiarla al entrar.
    """
    membership = _empleado_gestionable(request, membership_id)
    if membership is None:
        return redirect("employees:list")

    clave_temporal = reset_employee_access(membership)

    audit.record(
        request,
        AuditAction.PASSWORD_RESET_REQUESTED,
        store=request.store,
        object_type="User",
        object_id=str(membership.user_id),
        metadata={"email": membership.user.email, "motivo": "restablecido_por_el_dueno"},
    )

    request.session[SESSION_KEY_CREDENCIAL] = {
        "email": membership.user.email,
        "password": clave_temporal,
        "nombre": membership.user.full_name,
        "motivo": "restablecimiento",
    }
    return redirect("employees:list")


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------

def _empleado_gestionable(
    request: HttpRequest, membership_id: uuid.UUID
) -> Membership | None:
    """Devuelve la membresia si el dueno puede actuar sobre ella, o ``None``.

    Tres barreras, en este orden:

    1. **Solo la tienda activa.** El filtro por ``store`` hace que una
       membresia de otro negocio devuelva 404, no 403: no se confirma
       siquiera que ese identificador exista.
    2. **Ni sobre uno mismo.** Un dueno que se desactiva se queda fuera de su
       propio negocio sin nadie que pueda devolverle el acceso.
    3. **Solo cajeros.** Un propietario no administra a otro propietario;
       eso corresponde a la organizacion, no a la pantalla de empleados.

    Devuelve ``None`` en los casos 2 y 3, con el aviso ya puesto, para que la
    vista redirija. El caso 1 corta antes con un 404.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("user"),
        id=membership_id,
        store=request.store,
    )

    if membership.user_id == request.user.id:
        messages.error(request, "No puedes cambiar tu propio acceso desde aqui.")
        return None

    if membership.role != Role.CASHIER:
        messages.error(request, "Desde esta pantalla solo se administran cajeros.")
        return None

    return membership
