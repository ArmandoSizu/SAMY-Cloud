"""Comprueba que un alta creada desde el navegador quedo COMPLETA y correcta.

Se ejecuta dentro del contenedor del Core y mira la base de datos real, no la
respuesta HTTP: la unica prueba valida de que un registro funciono es que las
cuatro entidades existan y esten bien ligadas.

Uso:
    docker compose exec -T core python verificar_alta.py correo@negocio.mx
"""

from __future__ import annotations

import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from apps.audit.models import AuditEvent  # noqa: E402
from apps.tenancy.models import Membership, Role  # noqa: E402

User = get_user_model()

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FALLA {label}" + (f"  -> {detail}" if detail else ""))


email = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
if not email:
    print("Uso: python verificar_alta.py correo@negocio.mx")
    sys.exit(2)

print(f"\n{'=' * 62}\n  ALTA DE {email}\n{'=' * 62}")

usuario = User.objects.filter(email=email).first()
if usuario is None:
    print(f"  No existe ningun usuario con el correo {email}.")
    sys.exit(1)

print(f"  Nombre:   {usuario.full_name}")
print(f"  Telefono: {usuario.phone}")

check("el usuario existe y esta activo", usuario.is_active)
check(
    "NO es administrador de plataforma",
    not usuario.is_platform_admin,
    "se creo un administrador desde el registro publico",
)
check("NO es staff", not usuario.is_staff)
check("NO es superusuario", not usuario.is_superuser)
check(
    "el telefono quedo normalizado a 10 digitos",
    usuario.phone.isdigit() and len(usuario.phone) == 10,
    usuario.phone,
)
check(
    "la contrasena esta cifrada (no en claro)",
    usuario.password.startswith(("argon2", "pbkdf2")),
    usuario.password[:20],
)
check(
    "el propietario NO arrastra cambio de contrasena obligatorio",
    not usuario.must_change_password,
)

membresia = Membership.objects.filter(user=usuario).select_related(
    "store", "store__organization"
).first()
if membresia is None:
    print("  FALLA  el usuario quedo SIN membresia (cuenta a medio crear)")
    sys.exit(1)

tienda = membresia.store
organizacion = tienda.organization

print(f"  Organizacion: {organizacion.name}  (slug: {organizacion.slug})")
print(f"  Tienda:       {tienda.name}  (codigo: {tienda.code})")
print(f"  Ciudad:       {tienda.city}, {tienda.state} {tienda.postal_code}")
print(f"  Rol:          {membresia.get_role_display()}")

check("el rol es STORE_OWNER", membresia.role == Role.STORE_OWNER, membresia.role)
check("la membresia esta activa", membresia.is_active)
check("es la tienda predeterminada", membresia.is_default)
check("la tienda pertenece a la organizacion", tienda.organization_id == organizacion.id)
check("la organizacion esta activa", organizacion.is_active)
check("la tienda esta activa", tienda.is_active)

import re  # noqa: E402

check(
    "el codigo de tienda cumple el formato del folio",
    bool(re.fullmatch(r"[A-Z0-9\-]{3,12}", tienda.code)),
    tienda.code,
)

check("puede administrar empleados", membresia.has_perm("store.manage_employees"))
check("puede operar", membresia.has_perm("operation.create"))
check(
    "NO tiene permisos de plataforma",
    not membresia.has_perm("platform.manage_providers"),
)

evento = AuditEvent.objects.filter(
    actor=usuario, new_state="STORE_OWNER"
).first()
check("el alta quedo en la bitacora", evento is not None)
if evento is not None:
    check(
        "la bitacora dice que fue un alta publica",
        evento.metadata.get("motivo") == "alta_publica",
        str(evento.metadata),
    )

print(f"\n  PASADAS: {PASS}    FALLIDAS: {FAIL}\n")
sys.exit(1 if FAIL else 0)
