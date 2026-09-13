"""Importa el catalogo REAL de un proveedor a ``ProviderCatalogItem``.

    python manage.py importar_catalogo_proveedor --proveedor taecel

Lo que hace: pide ``provider.fetch_catalog()`` y guarda lo que devuelva, tal
como lo devuelva.

Lo que NO hace, y es lo importante:

* **No inventa filas.** Si el proveedor no esta listo, el comando falla y no
  escribe nada. Un catalogo semilla "de ejemplo" acaba emparejado con
  productos reales y vendido a clientes reales.
* **No habilita nada.** Importar no es aprobar. Las filas que crea no son
  vendibles por si solas; hace falta un mapping revisado por una persona.
* **No sobreescribe en silencio.** Si un SKU vuelve con otra identidad
  (otro importe, otro nombre), los mappings que dependian de el pasan a
  REVIEW_REQUIRED.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.commercial.models import (
    Environment,
    ProviderCatalogItem,
    ProviderProductMapping,
)
from apps.providers.registry import get_provider
from samy_common.providers.base import ProviderMode
from samy_common.providers.exceptions import ProviderNotConfigured


class Command(BaseCommand):
    help = "Importa el catalogo real de un proveedor de recargas."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--proveedor", required=True, help="slug: taecel, reloadly")
        parser.add_argument(
            "--seco",
            action="store_true",
            help="Muestra lo que traeria el proveedor sin escribir nada.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        slug = str(options["proveedor"]).strip().lower()
        seco = bool(options["seco"])

        proveedor = get_provider(slug)

        # El ambiente lo dice el adaptador, no un parametro. Que alguien pueda
        # escribir "--ambiente PRODUCTION" mientras el adaptador apunta al
        # sandbox es como se acaba con SKUs de prueba etiquetados como
        # productivos.
        ambiente = (
            Environment.PRODUCTION
            if proveedor.mode == ProviderMode.PRODUCTION
            else Environment.SANDBOX
        )

        salud = proveedor.check_health()
        self.stdout.write(f"Proveedor : {slug}")
        self.stdout.write(f"Ambiente  : {ambiente}")
        self.stdout.write(f"Estado    : {salud.status}")

        if not salud.is_operational:
            # Se detiene aqui a proposito, sin escribir una sola fila.
            raise CommandError(
                f"'{slug}' no esta operativo ({salud.status}): {salud.detail}\n"
                "No se importa nada: un catalogo inventado es peor que no tener "
                "catalogo."
            )

        try:
            productos = proveedor.fetch_catalog()
        except ProviderNotConfigured as exc:
            raise CommandError(f"'{slug}' no esta configurado: {exc}") from exc

        if not productos:
            raise CommandError(
                f"'{slug}' respondio con un catalogo vacio. No se borra ni se "
                "desactiva nada por una respuesta vacia: podria ser un fallo "
                "suyo, y desactivar el catalogo dejaria de vender todo."
            )

        self.stdout.write(f"Productos devueltos: {len(productos)}")

        if seco:
            for p in productos[:25]:
                self.stdout.write(
                    f"  {p.provider_product_id:24} {p.operator_name:20} "
                    f"{p.label[:40]:40} {p.amount or 'monto libre'}"
                )
            if len(productos) > 25:
                self.stdout.write(f"  ... y {len(productos) - 25} mas")
            self.stdout.write(self.style.WARNING("Marcha en seco: no se escribio nada."))
            return

        self._guardar(slug, ambiente, productos)

    @transaction.atomic
    def _guardar(self, slug: str, ambiente: str, productos: list[Any]) -> None:
        ahora = timezone.now()
        vistos: set[str] = set()
        nuevos = 0
        cambiados = 0
        mappings_a_revision = 0

        for producto in productos:
            sku = str(producto.provider_product_id).strip()
            if not sku:
                continue
            vistos.add(sku)

            item, creado = ProviderCatalogItem.objects.get_or_create(
                provider_slug=slug,
                environment=ambiente,
                provider_product_id=sku,
                defaults={"first_seen_at": ahora},
            )

            huella_anterior = item.fingerprint

            item.provider_operator = producto.operator_name or producto.operator_code
            item.provider_family = str(producto.raw.get("family") or "")
            item.provider_product_name = producto.label
            item.amount_cents = (
                producto.amount.cents if producto.amount is not None else None
            )
            item.currency = producto.amount.currency if producto.amount else "MXN"
            # Un producto de denominacion fija lleva el importe en el SKU. El
            # adaptador lo necesita para NO mandar el monto por separado.
            item.amount_in_sku = producto.amount is not None
            item.raw = dict(producto.raw)
            item.active = True
            item.last_seen_at = ahora
            item.fingerprint = item.calcular_fingerprint()
            item.save()

            if creado:
                nuevos += 1
            elif huella_anterior and huella_anterior != item.fingerprint:
                cambiados += 1
                # Cambio la identidad bajo nuestros pies. Los mappings que
                # apuntaban aqui NO se actualizan solos.
                for mapping in ProviderProductMapping.objects.filter(
                    provider_slug=slug, environment=ambiente, provider_product_id=sku
                ):
                    mapping.marcar_para_revision(
                        f"El proveedor cambio la identidad del SKU {sku}."
                    )
                    mappings_a_revision += 1

        # Lo que el proveedor ya no trae se marca inactivo, no se borra: una
        # fila retirada sigue explicando por que un mapping dejo de servir.
        retirados = (
            ProviderCatalogItem.objects.filter(
                provider_slug=slug, environment=ambiente, active=True
            )
            .exclude(provider_product_id__in=vistos)
            .update(active=False)
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Nuevos            : {nuevos}"))
        self.stdout.write(f"Identidad cambiada: {cambiados}")
        self.stdout.write(f"Retirados         : {retirados}")
        if mappings_a_revision:
            self.stdout.write(
                self.style.WARNING(
                    f"Mappings a revision: {mappings_a_revision} "
                    "(el proveedor cambio el producto; no se vende hasta revisarlos)"
                )
            )
        self.stdout.write("")
        self.stdout.write(
            "Importar NO habilita nada. Siguiente paso: "
            f"manage.py emparejar_catalogo_proveedor --proveedor {slug}"
        )
