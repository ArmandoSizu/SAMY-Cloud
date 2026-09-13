"""Empareja el catalogo comercial con el del proveedor, por identidad exacta.

    python manage.py emparejar_catalogo_proveedor --proveedor taecel
    python manage.py emparejar_catalogo_proveedor --proveedor taecel --guardar

**Por omision no escribe nada.** Se corre en seco, se lee el reporte, y solo
entonces se agrega ``--guardar``. Y aun con ``--guardar`` los mappings nacen
en REVIEW_REQUIRED y deshabilitados: este comando propone, no autoriza.

El reporte sale en el orden en que hay que trabajarlo: Telcel primero, y
dentro de cada familia el importe menor primero, que es la prioridad
acordada (TELCEL -> Amigo Sin Limite -> $100 -> $200).

La columna que hay que leer con atencion es "descartados por precio": son los
SKUs del proveedor que tienen el mismo importe y NO son el producto. Si el
emparejamiento se hiciera por precio, se habria elegido uno de esos.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.commercial.mapping import Propuesta, emparejar_catalogo, guardar
from apps.commercial.models import Environment, ProviderCatalogItem
from apps.providers.registry import get_provider
from samy_common.providers.base import ProviderMode


class Command(BaseCommand):
    help = "Propone mappings de proveedor por identidad exacta."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--proveedor", required=True)
        parser.add_argument(
            "--guardar",
            action="store_true",
            help="Escribe las propuestas como mappings EN REVISION (no habilitados).",
        )
        parser.add_argument(
            "--operador", default="", help="Limitar a un operador (ej. TELCEL)."
        )
        parser.add_argument(
            "--ambiente",
            default="",
            help="SANDBOX o PRODUCTION. Por omision, el del adaptador.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        slug = str(options["proveedor"]).strip().lower()
        escribir = bool(options["guardar"])
        operador = str(options["operador"]).strip()

        ambiente = str(options["ambiente"]).strip().upper()
        if not ambiente:
            proveedor = get_provider(slug)
            ambiente = (
                Environment.PRODUCTION
                if proveedor.mode == ProviderMode.PRODUCTION
                else Environment.SANDBOX
            )
        if ambiente not in Environment.values:
            raise CommandError(f"Ambiente invalido: {ambiente}")

        disponibles = ProviderCatalogItem.objects.filter(
            provider_slug=slug, environment=ambiente, active=True
        ).count()
        self.stdout.write(f"Proveedor: {slug}   Ambiente: {ambiente}")
        self.stdout.write(f"Productos del proveedor en catalogo: {disponibles}")
        if not disponibles:
            raise CommandError(
                f"No hay catalogo importado de '{slug}' en {ambiente}. Corre "
                f"primero: manage.py importar_catalogo_proveedor --proveedor {slug}"
            )

        propuestas = emparejar_catalogo(
            provider_slug=slug, environment=ambiente, solo_operador=operador
        )
        self._reportar(propuestas, escribir=escribir)

    def _reportar(self, propuestas: list[Propuesta], *, escribir: bool) -> None:
        con, sin = [], []
        for p in propuestas:
            (con if p.hay_coincidencia else sin).append(p)

        self.stdout.write("")
        self.stdout.write(f"=== COINCIDENCIA EXACTA ({len(con)}) ===")
        for p in con:
            item = p.elegido
            assert item is not None
            self.stdout.write(
                f"  {p.producto.commercial_name:34} -> {item.provider_product_id:20} "
                f"[{item.provider_family or 'sin familia'} / "
                f"{item.provider_product_name[:30]}]"
            )
            if p.descartados_por_precio:
                # Esta linea es el argumento entero del modulo.
                self.stdout.write(
                    self.style.WARNING(
                        "      por precio habria elegido tambien: "
                        + ", ".join(
                            sorted(
                                c.provider_product_id for c in p.descartados_por_precio
                            )[:6]
                        )
                    )
                )

        self.stdout.write("")
        self.stdout.write(f"=== SIN COINCIDENCIA ({len(sin)}) ===")
        por_motivo: dict[str, list[Propuesta]] = {}
        for p in sin:
            por_motivo.setdefault(str(p.motivo), []).append(p)
        for motivo, grupo in sorted(por_motivo.items()):
            self.stdout.write(f"  {motivo}: {len(grupo)}")
            # Un ejemplo por motivo. El detalle dice que hay que arreglar.
            self.stdout.write(f"      ej. {grupo[0].producto.commercial_name}")
            self.stdout.write(f"          {grupo[0].detalle}")

        if not escribir:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Marcha en seco: no se escribio nada. Agrega --guardar para "
                    "crear los mappings (naceran EN REVISION y deshabilitados)."
                )
            )
            return

        guardados = sum(1 for p in con if guardar(p) is not None)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Mappings escritos: {guardados}"))
        self.stdout.write(
            self.style.WARNING(
                "TODOS quedaron en REVIEW_REQUIRED y enabled=False. Ningun "
                "producto se volvio vendible. Hay que aprobarlos uno por uno en "
                "el panel de plataforma, comparando nuestra fila con la del "
                "proveedor."
            )
        )
