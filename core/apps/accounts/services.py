"""Alta de cuentas y de empleados.

Toda la logica vive aqui y no en las vistas por dos razones:

1. **El rol se decide en el servidor, siempre.** Ninguna funcion de este
   modulo acepta un rol como parametro desde fuera: ``register_owner`` crea
   STORE_OWNER y ``create_cashier`` crea CASHIER, y punto. Si el rol llegara
   como argumento, bastaria un campo oculto manipulado para fabricar un
   PLATFORM_ADMIN.
2. **O se crea todo o no se crea nada.** Un usuario sin organizacion, o una
   tienda sin dueno, dejan la cuenta en un estado del que la interfaz no sabe
   salir. Las cuatro entidades se escriben dentro de una transaccion.
"""

from __future__ import annotations

import secrets
import string
import unicodedata
from dataclasses import dataclass

import structlog
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.text import slugify

from apps.tenancy.models import Membership, Organization, Role, Store

log = structlog.get_logger("accounts.services")

User = get_user_model()

#: Alfabeto de las contrasenas temporales. Sin caracteres que se confundan al
#: dictarlas en voz alta o al copiarlas de un papel: O/0, I/l/1.
_TEMP_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits if c not in "O0Il1"
)


@dataclass(frozen=True, slots=True)
class OwnerRegistration:
    """Resultado del alta de un negocio nuevo."""

    user: User
    organization: Organization
    store: Store
    membership: Membership


# ---------------------------------------------------------------------------
# Alta publica de un negocio
# ---------------------------------------------------------------------------

@transaction.atomic
def register_owner(
    *,
    email: str,
    password_hash: str,
    first_name: str,
    last_name: str,
    phone: str,
    organization_name: str,
    store_name: str,
    store_phone: str = "",
    city: str = "",
    state: str = "",
    postal_code: str = "",
) -> OwnerRegistration:
    """Crea usuario + organizacion + tienda + membresia de propietario.

    Recibe la contrasena **ya cifrada**. El texto plano no llega hasta aqui:
    se cifra en cuanto se valida el paso 1, de modo que entre pantalla y
    pantalla lo unico que viaja en la sesion es un hash.

    El usuario se crea SIEMPRE con ``is_platform_admin=False`` e
    ``is_staff=False``, sin importar lo que traiga la peticion. El registro
    publico no es un camino para escalar privilegios.
    """
    user = User(
        email=email.strip().lower(),
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        phone=phone.strip(),
        # Explicito, no por omision: se lee de un vistazo en la revision de
        # seguridad que por aqui no sale un administrador.
        is_platform_admin=False,
        is_staff=False,
        is_superuser=False,
        is_active=True,
    )
    user.password = password_hash
    user.full_clean(exclude=["password"])
    user.save()

    organization = Organization.objects.create(
        name=organization_name.strip(),
        slug=_unique_org_slug(organization_name),
    )

    store = Store.objects.create(
        organization=organization,
        name=store_name.strip(),
        code=_unique_store_code(organization, store_name),
        city=city.strip(),
        state=state.strip(),
        postal_code=postal_code.strip(),
        phone=store_phone.strip(),
    )

    membership = Membership.objects.create(
        user=user,
        store=store,
        role=Role.STORE_OWNER,
        is_active=True,
        is_default=True,
    )

    log.info(
        "owner_registered",
        user_id=str(user.id),
        organization_id=str(organization.id),
        store_id=str(store.id),
    )
    return OwnerRegistration(
        user=user, organization=organization, store=store, membership=membership
    )


# ---------------------------------------------------------------------------
# Alta de empleados
# ---------------------------------------------------------------------------

@transaction.atomic
def create_cashier(
    *,
    store: Store,
    email: str,
    first_name: str,
    last_name: str,
    phone: str = "",
) -> tuple[Membership, str]:
    """Da de alta un cajero en ``store``. Devuelve (membresia, clave temporal).

    La clave temporal se genera aqui y se muestra UNA sola vez al dueno, que
    se la entrega al cajero. No se guarda en claro en ningun sitio: lo que
    queda en la base es su hash, como cualquier otra contrasena.

    Se marca ``must_change_password`` para que el cajero tenga que cambiarla
    en su primer acceso; el middleware lo obliga. Una clave que dicto otra
    persona no puede quedarse puesta.

    Si el correo ya existe en la plataforma (una persona que trabaja en dos
    tiendas), se reutiliza el usuario y solo se anade la membresia. Crear un
    segundo usuario con el mismo correo es imposible por la restriccion unica,
    y ademas seria incorrecto: es la misma persona.
    """
    email = email.strip().lower()
    temporary_password = generate_temporary_password()

    user = User.objects.filter(email=email).first()
    created_user = user is None

    if created_user:
        user = User(
            email=email,
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            phone=phone.strip(),
            is_platform_admin=False,
            is_staff=False,
            is_superuser=False,
            is_active=True,
            must_change_password=True,
        )
        user.set_password(temporary_password)
        user.full_clean(exclude=["password"])
        user.save()

    membership, membership_created = Membership.objects.get_or_create(
        user=user,
        store=store,
        defaults={"role": Role.CASHIER, "is_active": True},
    )
    if not membership_created:
        # Ya trabajaba aqui y se le habia dado de baja: se reactiva en vez de
        # fallar. Reactivar es lo que el dueno quiere decir al volver a darlo
        # de alta con el mismo correo.
        membership.is_active = True
        membership.save(update_fields=["is_active", "updated_at"])

    log.info(
        "cashier_created",
        user_id=str(user.id),
        store_id=str(store.id),
        nuevo_usuario=created_user,
    )
    # Si el usuario ya existia, su contrasena NO se toca: tiene la suya.
    return membership, (temporary_password if created_user else "")


@transaction.atomic
def reset_employee_access(membership: Membership) -> str:
    """Genera una clave temporal nueva y obliga a cambiarla. Devuelve la clave.

    Es la salida para el caso mas comun del mostrador: el cajero olvido su
    contrasena y el correo de recuperacion no le llega o no tiene correo a
    mano. El dueno le entrega una clave nueva y el sistema le obliga a
    cambiarla al entrar.
    """
    temporary_password = generate_temporary_password()
    user = membership.user
    user.set_password(temporary_password)
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])
    log.info("employee_access_reset", user_id=str(user.id), store_id=str(membership.store_id))
    return temporary_password


def generate_temporary_password(length: int = 14) -> str:
    """Clave temporal aleatoria.

    ``secrets`` y no ``random``: el segundo usa un generador predecible que no
    sirve para nada que proteja una cuenta.
    """
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))


# ---------------------------------------------------------------------------
# Identificadores derivados del nombre
# ---------------------------------------------------------------------------

def _unique_org_slug(name: str) -> str:
    """Slug de organizacion, unico en toda la plataforma.

    ``Organization.slug`` es unico globalmente, asi que dos negocios que se
    llamen igual (algo nada raro: "Abarrotes La Esperanza") chocarian. Se
    anade un sufijo numerico en vez de dejar que reviente la insercion.
    """
    base = slugify(name)[:150] or "negocio"
    slug = base
    counter = 2
    while Organization.objects.filter(slug=slug).exists():
        sufijo = f"-{counter}"
        slug = f"{base[: 160 - len(sufijo)]}{sufijo}"
        counter += 1
    return slug


def _unique_store_code(organization: Organization, store_name: str) -> str:
    """Codigo corto de la tienda, unico dentro de la organizacion.

    Este codigo sale impreso en el folio del comprobante, asi que se deriva
    del nombre para que el dueno lo reconozca: "Sucursal Centro" -> CENTRO.
    Debe cumplir ^[A-Z0-9-]{3,12}$.
    """
    limpio = _ascii_upper(store_name)
    palabras = [p for p in limpio.split() if p not in {"SUCURSAL", "TIENDA", "LA", "EL", "DE", "DEL", "LOS", "LAS"}]
    base = "".join(palabras) if palabras else limpio.replace(" ", "")
    base = "".join(c for c in base if c.isalnum())[:12]

    if len(base) < 3:
        base = (base + "TIENDA")[:6]

    code = base
    counter = 2
    while Store.objects.filter(organization=organization, code=code).exists():
        sufijo = str(counter)
        code = f"{base[: 12 - len(sufijo)]}{sufijo}"
        counter += 1
    return code


def _ascii_upper(texto: str) -> str:
    """Mayusculas sin acentos ni enes. El codigo de tienda solo admite A-Z0-9."""
    normalizado = unicodedata.normalize("NFKD", texto)
    sin_acentos = "".join(c for c in normalizado if not unicodedata.combining(c))
    return sin_acentos.upper()
