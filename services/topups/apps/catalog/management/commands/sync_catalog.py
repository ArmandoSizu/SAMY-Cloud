"""Sincroniza el catalogo de recargas desde el proveedor.

    python manage.py sync_catalog

Si no hay credenciales, lo dice y explica que falta. NO deja el catalogo con
datos de ejemplo.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.catalog.sync import sync_catalog
from samy_common.providers.exceptions import ProviderError


class Command(BaseCommand):
    help = "Descarga operadores y denominaciones reales desde el proveedor."

    def add_arguments(self, parser):
        parser.add_argument("--provider", default=None, help="Slug del proveedor.")

    def handle(self, *args, **options):
        self.stdout.write("Sincronizando catalogo...")
        try:
            run = sync_catalog(options["provider"])
        except ProviderError as exc:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("  No se pudo sincronizar el catalogo."))
            self.stdout.write(f"  Proveedor: {exc.provider}")
            self.stdout.write(f"  Motivo:    {exc.message}")
            missing = getattr(exc, "missing_requirements", ())
            if missing:
                self.stdout.write("")
                self.stdout.write("  Falta:")
                for item in missing:
                    self.stdout.write(f"    - {item}")
            self.stdout.write("")
            self.stdout.write(
                "  El catalogo queda vacio a proposito. Ver docs/api-integrations.md."
            )
            return

        self.stdout.write(self.style.SUCCESS("  Catalogo sincronizado:"))
        self.stdout.write(f"    Operadores:   {run.operators_found}")
        self.stdout.write(f"    Productos:    {run.products_found}")
        self.stdout.write(f"    Nuevos:       {run.products_created}")
        self.stdout.write(f"    Actualizados: {run.products_updated}")
        self.stdout.write(f"    Desactivados: {run.products_deactivated}")
