"""Crea los datos minimos para operar: una organizacion, una tienda y usuarios.

Lo que este comando SI crea:
  - Una organizacion y una tienda reales, vacias.
  - Un usuario por cada rol, con contraseñas que se imprimen en pantalla.

Lo que este comando NO crea, deliberadamente:
  - Transacciones, ordenes ni historial. El dashboard debe arrancar en cero y
    mostrar su estado vacio. Sembrar operaciones falsas para que las graficas
    "se vean bien" es exactamente lo que este proyecto no hace: un numero
    inventado en un panel de dinero destruye la confianza en todo lo demas.
  - Catalogo de recargas. Ese viene del proveedor o no viene.

Es idempotente: ejecutarlo dos veces no duplica nada.
"""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.tenancy.models import Membership, Organization, Role, Store

User = get_user_model()

#: Contraseñas fijas solo si se pasa --dev. En cualquier otro caso se generan
#: al azar y se imprimen una sola vez.
DEV_PASSWORD = "SamyCloud2026!"


class Command(BaseCommand):
    help = "Crea la organizacion, tienda y usuarios iniciales de SAMY Cloud."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dev",
            action="store_true",
            help="Usa una contraseña fija conocida. SOLO para desarrollo local.",
        )
        parser.add_argument(
            "--org", default="Abarrotes La Esperanza", help="Nombre de la organizacion."
        )
        parser.add_argument("--store", default="Sucursal Centro", help="Nombre de la tienda.")
        parser.add_argument("--code", default="CENTRO", help="Codigo corto de la tienda.")

    @transaction.atomic
    def handle(self, *args, **options):
        dev_mode = options["dev"]

        organization, org_created = Organization.objects.get_or_create(
            slug="abarrotes-la-esperanza",
            defaults={"name": options["org"], "is_active": True},
        )
        self._report("Organizacion", organization.name, org_created)

        store, store_created = Store.objects.get_or_create(
            organization=organization,
            code=options["code"],
            defaults={
                "name": options["store"],
                "city": "Manzanillo",
                "state": "Colima",
                "is_active": True,
            },
        )
        self._report("Tienda", f"{store.name} ({store.code})", store_created)

        credenciales = []

        for email, nombre, apellido, rol, es_plataforma in [
            ("admin@samycloud.mx", "Admin", "SAMY Cloud", None, True),
            ("dueno@laesperanza.mx", "Carmen", "Nunez", Role.STORE_OWNER, False),
            ("cajero@laesperanza.mx", "Luis", "Ramirez", Role.CASHIER, False),
        ]:
            usuario = User.objects.filter(email=email).first()
            creado = usuario is None

            if creado:
                password = DEV_PASSWORD if dev_mode else secrets.token_urlsafe(12)
                usuario = User.objects.create_user(
                    email=email,
                    password=password,
                    first_name=nombre,
                    last_name=apellido,
                    is_platform_admin=es_plataforma,
                    is_staff=es_plataforma,
                    is_superuser=es_plataforma,
                )
                credenciales.append((email, password, rol or "PLATFORM_ADMIN"))
            else:
                credenciales.append((email, "(sin cambios)", rol or "PLATFORM_ADMIN"))

            if rol is not None:
                membership, m_creada = Membership.objects.get_or_create(
                    user=usuario,
                    store=store,
                    defaults={"role": rol, "is_active": True, "is_default": True},
                )
                self._report(
                    f"Membresia {rol}", f"{usuario.email} @ {store.code}", m_creada
                )

            self._report("Usuario", f"{usuario.email} ({rol or 'PLATFORM_ADMIN'})", creado)

        self._print_credentials(credenciales, dev_mode)

    # -- salida --------------------------------------------------------

    def _report(self, tipo: str, nombre: str, creado: bool) -> None:
        if creado:
            self.stdout.write(self.style.SUCCESS(f"  + {tipo}: {nombre}"))
        else:
            self.stdout.write(f"  = {tipo}: {nombre} (ya existia)")

    def _print_credentials(self, credenciales, dev_mode: bool) -> None:
        self.stdout.write("")
        self.stdout.write("=" * 64)
        self.stdout.write(self.style.WARNING("  CREDENCIALES DE ACCESO"))
        self.stdout.write("=" * 64)
        for email, password, rol in credenciales:
            self.stdout.write(f"  {rol:<16} {email:<28} {password}")
        self.stdout.write("=" * 64)

        if dev_mode:
            self.stdout.write(
                self.style.ERROR(
                    "  Contrasena fija de DESARROLLO. No la uses en produccion."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "  Estas contrasenas no se vuelven a mostrar. Guardalas ahora."
                )
            )
        self.stdout.write("")
        self.stdout.write(
            "  El dashboard arranca SIN operaciones: es su estado vacio real,"
        )
        self.stdout.write("  no un error. No se siembran transacciones ficticias.")
        self.stdout.write("")
