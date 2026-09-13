"""Reporte del catalogo comercial contra el catalogo de un proveedor.

    python manage.py reporte_catalogo_proveedor --proveedor linntae
    python manage.py reporte_catalogo_proveedor --proveedor linntae --json
    python manage.py reporte_catalogo_proveedor --proveedor linntae --solo-bloqueados

Es un reporte, no una accion: **no escribe nada**. Responde a una sola
pregunta, producto por producto:

    ¿esto se puede vender hoy por este proveedor, y si no, por que no?

LOS TRES ESTADOS
----------------

``MAPPED``
    Hay mapping habilitado, sin observaciones y con identidad completa. Es el
    unico que permite cobrar.

``REVIEW_REQUIRED``
    Hay un candidato -o incluso un mapping ya creado- pero falta la
    aprobacion humana, o el proveedor cambio el producto bajo nuestros pies.
    **No se vende.**

``NOT_AVAILABLE``
    El proveedor no tiene nada que corresponda a este producto. **No se
    vende.**

Y el motivo se dice completo, porque "no se pudo emparejar" a secas obliga a
reconstruir el razonamiento a mano sobre cientos de filas: el reporte dice si
lo que falta es un alias del operador, un alias de la familia o un importe que
el proveedor no ofrece.

LA COMISION QUE APARECE EN CADA FILA
------------------------------------

Sale de ``ProviderCommission``, o sea de lo que el proveedor respondio y se
guardo con fecha. Si no hay fila, la columna dice ``desconocida`` y no un
porcentaje plausible. Un margen inventado es peor que ningun margen: da
confianza falsa sobre si el negocio gana o pierde.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.commercial.mapping import Rechazo, emparejar_catalogo
from apps.commercial.models import (
    AlcanceComision,
    Environment,
    ProviderCommission,
    ProviderProductMapping,
)
from apps.providers.registry import get_provider
from samy_common.providers.base import ProviderMode

MAPPED = "MAPPED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
NOT_AVAILABLE = "NOT_AVAILABLE"


class Command(BaseCommand):
    help = "Reporte de mapping del catalogo comercial contra un proveedor."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--proveedor", default="linntae", help="slug del proveedor")
        parser.add_argument("--operador", default="", help="filtra por codigo de operador")
        parser.add_argument(
            "--solo-bloqueados",
            action="store_true",
            dest="solo_bloqueados",
            help="Muestra unicamente lo que hoy NO se puede vender.",
        )
        parser.add_argument("--json", action="store_true", dest="como_json")

    def handle(self, *args: Any, **options: Any) -> None:
        slug = str(options["proveedor"]).strip().lower()
        solo_operador = str(options["operador"]).strip()
        solo_bloqueados = bool(options["solo_bloqueados"])
        como_json = bool(options["como_json"])

        proveedor = get_provider(slug)
        ambiente = (
            Environment.PRODUCTION
            if proveedor.mode == ProviderMode.PRODUCTION
            else Environment.SANDBOX
        )

        comisiones = self._comisiones_por_clave(slug, ambiente)
        mappings = self._mappings_por_producto(slug, ambiente)

        propuestas = emparejar_catalogo(
            provider_slug=slug, environment=ambiente, solo_operador=solo_operador
        )

        filas: list[dict[str, Any]] = []
        for propuesta in propuestas:
            producto = propuesta.producto
            mapping = mappings.get(str(producto.id))
            elegido = propuesta.elegido

            estado, motivo = self._estado(propuesta, mapping)

            id_operator = ""
            id_offer = ""
            descripcion = ""
            if elegido is not None:
                id_operator = str(elegido.raw.get("idOperator") or "")
                id_offer = elegido.provider_product_id
                descripcion = elegido.provider_product_name
            elif mapping is not None:
                id_offer = mapping.provider_product_id
                descripcion = mapping.provider_product_name

            filas.append(
                {
                    "operador_comercial": producto.operator.code,
                    "familia_comercial": producto.family.code,
                    "producto_comercial": producto.commercial_name,
                    "monto": str(producto.price),
                    "monto_cents": producto.price_cents,
                    "id_operator": id_operator,
                    "id_offer": id_offer,
                    "descripcion_proveedor": descripcion,
                    "comision": self._comision_de(
                        comisiones, id_operator, producto.operator.name, id_offer
                    ),
                    "mapping_status": estado,
                    "motivo": motivo,
                    "descartados_por_precio": [
                        c.provider_product_id for c in propuesta.descartados_por_precio
                    ],
                }
            )

        if solo_bloqueados:
            filas = [f for f in filas if f["mapping_status"] != MAPPED]

        resumen = {
            "proveedor": slug,
            "ambiente": str(ambiente),
            "modo": str(proveedor.mode),
            "generado_en": timezone.now().isoformat(),
            "total": len(filas),
            "mapped": sum(1 for f in filas if f["mapping_status"] == MAPPED),
            "review_required": sum(
                1 for f in filas if f["mapping_status"] == REVIEW_REQUIRED
            ),
            "not_available": sum(
                1 for f in filas if f["mapping_status"] == NOT_AVAILABLE
            ),
        }

        if como_json:
            self.stdout.write(
                json.dumps({"resumen": resumen, "filas": filas}, indent=2, ensure_ascii=False)
            )
            return

        self._imprimir(resumen, filas)

    # -- datos auxiliares --------------------------------------------------

    def _comisiones_por_clave(self, slug: str, ambiente: str) -> dict[str, Any]:
        indice: dict[str, Any] = {}
        for fila in ProviderCommission.objects.filter(
            provider_slug=slug, environment=ambiente
        ):
            indice[f"{fila.alcance}:{fila.clave}".upper()] = fila
            if fila.nombre:
                indice[f"NOMBRE:{fila.nombre.strip().upper()}"] = fila
        return indice

    def _mappings_por_producto(
        self, slug: str, ambiente: str
    ) -> dict[str, ProviderProductMapping]:
        return {
            str(m.product_id): m
            for m in ProviderProductMapping.objects.filter(
                provider_slug=slug, environment=ambiente
            )
        }

    def _estado(
        self, propuesta: Any, mapping: ProviderProductMapping | None
    ) -> tuple[str, str]:
        """Estado y motivo. El orden importa: manda el mapping ya existente.

        Si hay mapping utilizable, el producto se vende por ahi y lo que el
        emparejador opine es secundario. Si hay mapping y NO es utilizable, el
        motivo del mapping es mas util que el del emparejador: dice por que
        lo sacaron de circulacion.
        """
        if mapping is not None:
            if mapping.es_utilizable:
                return MAPPED, "Mapping habilitado y con identidad completa."
            if not mapping.identidad_completa:
                return (
                    REVIEW_REQUIRED,
                    "El mapping existe pero le falta identidad (SKU, familia, "
                    "nombre del proveedor o importe declarado).",
                )
            return (
                REVIEW_REQUIRED,
                mapping.status_reason
                or f"Mapping en estado {mapping.status} y enabled={mapping.enabled}.",
            )

        if propuesta.hay_coincidencia:
            return (
                REVIEW_REQUIRED,
                "Hay un candidato con identidad exacta, sin mapping creado ni "
                "aprobado todavia.",
            )

        if propuesta.motivo in (Rechazo.SIN_CATALOGO,):
            return NOT_AVAILABLE, propuesta.detalle

        if propuesta.motivo in (
            Rechazo.OPERADOR_NO_RECONOCIDO,
            Rechazo.FAMILIA_NO_RECONOCIDA,
            Rechazo.AMBIGUO,
            Rechazo.SIN_SKU,
        ):
            # Estos son arreglables sin tocar al proveedor: falta un alias o
            # falta que una persona decida. Por eso son revision y no
            # "no disponible".
            return REVIEW_REQUIRED, propuesta.detalle

        return NOT_AVAILABLE, propuesta.detalle or "El proveedor no ofrece este producto."

    def _comision_de(
        self,
        indice: dict[str, Any],
        id_operator: str,
        nombre_operador: str,
        id_offer: str,
    ) -> str:
        """Comision guardada que corresponde a esta fila, o "desconocida".

        Se busca por SKU, luego por id de operador, luego por nombre. En ese
        orden porque lo especifico manda sobre lo general: si el proveedor
        declaro una tasa para el producto, esa es la que aplica.
        """
        for clave in (
            f"{AlcanceComision.SKU}:{id_offer}".upper() if id_offer else "",
            f"{AlcanceComision.OPERADOR}:{id_operator}".upper() if id_operator else "",
            f"NOMBRE:{nombre_operador.strip().upper()}" if nombre_operador else "",
        ):
            if clave and clave in indice:
                fila = indice[clave]
                if fila.tasa_bps is None:
                    return "ilegible"
                sufijo = "" if fila.exacta_en_bps else "~"
                return f"{fila.tasa_texto or fila.tasa_bps}{sufijo}"
        return "desconocida"

    # -- salida ------------------------------------------------------------

    def _imprimir(self, resumen: dict[str, Any], filas: list[dict[str, Any]]) -> None:
        self.stdout.write("")
        self.stdout.write(
            f"MAPPING {resumen['proveedor'].upper()} - {resumen['ambiente']}"
        )
        self.stdout.write("=" * 118)
        self.stdout.write(
            f"{'OPERADOR':10} {'FAMILIA':16} {'PRODUCTO':26} {'MONTO':>10} "
            f"{'idOp':>6} {'idOffer':>8} {'COMISION':>10} ESTADO"
        )
        self.stdout.write("-" * 118)

        for fila in filas:
            estado = fila["mapping_status"]
            estilo = (
                self.style.SUCCESS
                if estado == MAPPED
                else (self.style.WARNING if estado == REVIEW_REQUIRED else self.style.ERROR)
            )
            self.stdout.write(
                f"{fila['operador_comercial'][:10]:10} "
                f"{fila['familia_comercial'][:16]:16} "
                f"{fila['producto_comercial'][:26]:26} "
                f"{fila['monto']:>10} "
                f"{fila['id_operator'][:6]:>6} "
                f"{fila['id_offer'][:8]:>8} "
                f"{fila['comision'][:10]:>10} "
                + estilo(estado)
            )
            if estado != MAPPED and fila["motivo"]:
                self.stdout.write(f"{'':>10} -> {fila['motivo'][:100]}")
            if fila["descartados_por_precio"]:
                self.stdout.write(
                    f"{'':>10} -> coincidian SOLO en precio y se descartaron: "
                    + ", ".join(fila["descartados_por_precio"][:6])
                )

        self.stdout.write("-" * 118)
        self.stdout.write(
            f"Total {resumen['total']}   "
            + self.style.SUCCESS(f"MAPPED {resumen['mapped']}")
            + "   "
            + self.style.WARNING(f"REVIEW_REQUIRED {resumen['review_required']}")
            + "   "
            + self.style.ERROR(f"NOT_AVAILABLE {resumen['not_available']}")
        )
        self.stdout.write("")
        self.stdout.write(
            "Solo MAPPED se puede cobrar. REVIEW_REQUIRED y NOT_AVAILABLE no se venden."
        )
        self.stdout.write("")
