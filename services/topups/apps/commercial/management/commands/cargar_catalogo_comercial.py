"""Carga el catalogo comercial verificado.

    docker compose exec topups python manage.py cargar_catalogo_comercial

Es idempotente: se puede correr las veces que haga falta. Actualiza lo que
cambio y NO borra nada, porque las ordenes historicas apuntan a estos
productos y borrar uno dejaria comprobantes sin explicacion.

Tampoco crea mappings de proveedor. Eso es deliberado: un producto oficial no
es un producto vendible, y el mapping es una decision que exige saber que
identificador usa el proveedor. Inventarlo aqui seria justo lo contrario de lo
que este catalogo existe para evitar.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.commercial import catalogo_oficial
from apps.commercial.models import (
    CommercialFamily,
    CommercialOperator,
    CommercialProduct,
    CommercialProductVersion,
)


class Command(BaseCommand):
    help = "Carga o actualiza el catalogo comercial verificado de recargas."

    def handle(self, *args, **options) -> None:
        verificado_en = timezone.now()

        with transaction.atomic():
            operadores = self._operadores()
            familias = self._familias(operadores)
            creados, actualizados, versiones = self._productos(
                operadores, familias, verificado_en
            )

        self.stdout.write("")
        self.stdout.write(f"  operadores : {len(operadores)}")
        self.stdout.write(f"  familias   : {len(familias)}")
        self.stdout.write(f"  productos  : {creados} nuevos, {actualizados} actualizados")
        self.stdout.write(f"  versiones  : {versiones} vigentes")
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "  Ninguno es vendible todavia: falta el mapping de proveedor.\n"
                "  Es lo esperado. Ver docs/proveedores-recargas-mexico.md."
            )
        )

    def _operadores(self) -> dict[str, CommercialOperator]:
        salida = {}
        for fila in catalogo_oficial.OPERADORES:
            obj, _ = CommercialOperator.objects.update_or_create(
                code=fila["code"],
                defaults={
                    "name": fila["name"],
                    "display_priority": fila["display_priority"],
                    "is_primary": fila.get("is_primary", False),
                    "active": True,
                },
            )
            salida[fila["code"]] = obj
        return salida

    def _familias(
        self, operadores: dict[str, CommercialOperator]
    ) -> dict[tuple[str, str], CommercialFamily]:
        salida = {}
        for fila in catalogo_oficial.FAMILIAS:
            obj, _ = CommercialFamily.objects.update_or_create(
                operator=operadores[fila["operator"]],
                code=fila["code"],
                defaults={
                    "name": fila["name"],
                    "description": fila["description"],
                    "display_priority": fila["display_priority"],
                    "active": True,
                },
            )
            salida[(fila["operator"], fila["code"])] = obj
        return salida

    def _productos(self, operadores, familias, verificado_en) -> tuple[int, int, int]:
        creados = actualizados = versiones = 0

        for fila in catalogo_oficial.productos():
            operador = operadores[fila["operator"]]
            familia = familias[(fila["operator"], fila["family"])]

            producto, nuevo = CommercialProduct.objects.update_or_create(
                operator=operador,
                family=familia,
                commercial_name=fila["commercial_name"],
                defaults={
                    "price_cents": fila["price_cents"],
                    "currency": "MXN",
                    "active": True,
                    # Verificado significa "lo leimos en la fuente oficial",
                    # no "se puede vender". Son cosas distintas y esa
                    # distincion es la razon de ser de esta app.
                    "official_verified": True,
                    "official_source": fila["official_source"],
                    "official_tariff_reference": fila["official_tariff_reference"],
                    "verified_at": verificado_en,
                    "status": fila["status"],
                    "status_reason": fila.get("status_reason", ""),
                    "display_priority": fila["price_cents"] // 100,
                },
            )
            creados += int(nuevo)
            actualizados += int(not nuevo)

            datos = fila.get("version")
            if datos is None:
                # Producto sin cifras verificadas. No se le inventa una
                # version: se queda sin ella y en revision.
                continue

            versiones += 1
            self._version(producto, datos, fila, verificado_en)

        return creados, actualizados, versiones

    def _version(self, producto, datos, fila, verificado_en) -> None:
        """Crea la version vigente, o la actualiza si no cambio nada.

        Si los datos SI cambiaron, se crea una version nueva y la anterior deja
        de ser vigente pero se conserva: las ordenes viejas guardaron su
        snapshot y el comprobante historico tiene que seguir cuadrando.
        """
        vigente = producto.versions.filter(is_current=True).first()

        cambio = vigente is None or any(
            getattr(vigente, campo) != datos[campo]
            for campo in ("validity_days", "data_mb", "calls", "sms", "benefits", "restrictions")
        ) or vigente.price_cents != fila["price_cents"]

        if not cambio:
            return

        if vigente is not None:
            vigente.is_current = False
            vigente.save(update_fields=["is_current"])

        siguiente = (producto.versions.count() or 0) + 1
        CommercialProductVersion.objects.create(
            product=producto,
            version=siguiente,
            price_cents=fila["price_cents"],
            currency="MXN",
            validity_days=datos["validity_days"],
            data_mb=datos["data_mb"],
            calls=datos["calls"],
            sms=datos["sms"],
            benefits=datos["benefits"],
            restrictions=datos["restrictions"],
            official_source=fila["official_source"],
            official_tariff_reference=fila["official_tariff_reference"],
            verified_at=verificado_en,
            is_current=True,
        )
