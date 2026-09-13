"""Guarda las comisiones que el proveedor dice concedernos.

    python manage.py sincronizar_comisiones_proveedor --proveedor linntae
    python manage.py sincronizar_comisiones_proveedor --proveedor linntae --seco

Lee la respuesta real del proveedor y la escribe en ``ProviderCommission`` con
su texto crudo y la fecha. Nada mas.

LO QUE NO HACE
--------------

* **No inventa tasas.** Si el proveedor no responde, no se escribe nada. Una
  comision supuesta es el numero con el que alguien fijaria precios de verdad.
* **No concluye el mecanismo.** Las filas nacen con ``SIN_DETERMINAR`` salvo
  que la configuracion diga otra cosa explicitamente. Saber "5.5%" no es
  saber lo que cuesta una recarga: un descuento por transaccion, un bono al
  fondear y una comision abonada a otra bolsa dan tres costos distintos con
  el mismo porcentaje.
* **No borra.** Una tasa que el proveedor ya no reporta se queda, con su
  fecha, porque sigue explicando el margen de las ventas de esa epoca.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.commercial.models import (
    AlcanceComision,
    Environment,
    ProviderCommission,
)
from apps.providers.registry import get_provider
from samy_common.providers.base import ProviderMode
from samy_common.providers.exceptions import ProviderError

#: Mecanismo por omision. Ver el docstring del modulo.
MECANISMO_SIN_DETERMINAR = "SIN_DETERMINAR"


class Command(BaseCommand):
    help = "Sincroniza las comisiones reportadas por un proveedor de recargas."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--proveedor", default="linntae", help="slug del proveedor")
        parser.add_argument(
            "--seco",
            action="store_true",
            help="Muestra lo que traeria el proveedor sin escribir nada.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        slug = str(options["proveedor"]).strip().lower()
        seco = bool(options["seco"])

        proveedor = get_provider(slug)
        consultar = getattr(proveedor, "comisiones", None)
        if consultar is None:
            raise CommandError(
                f"El adaptador de '{slug}' no sabe consultar comisiones. No se "
                "inventa ninguna: sin ese endpoint, el margen queda "
                "desconocido y la cotizacion lo dice."
            )

        # El ambiente lo dice el adaptador, no un parametro: poder escribir
        # "--ambiente PRODUCTION" mientras apunta al sandbox es como se acaba
        # con tasas de prueba etiquetadas como productivas.
        ambiente = (
            Environment.PRODUCTION
            if proveedor.mode == ProviderMode.PRODUCTION
            else Environment.SANDBOX
        )

        from django.conf import settings

        mecanismo = str(
            getattr(settings, f"{slug.upper()}_COMMISSION_MECHANISM", "")
            or MECANISMO_SIN_DETERMINAR
        ).strip().upper()

        try:
            comisiones = consultar()
        except ProviderError as exc:
            raise CommandError(
                f"'{slug}' no pudo entregar sus comisiones: {exc.message}\n"
                "No se escribe nada."
            ) from exc

        if not comisiones:
            raise CommandError(
                f"'{slug}' respondio sin comisiones. No se borra ni se pone en "
                "cero nada por una respuesta vacia: cero seria afirmar que no "
                "hay comision."
            )

        self.stdout.write(f"Proveedor : {slug}")
        self.stdout.write(f"Ambiente  : {ambiente}")
        self.stdout.write(f"Mecanismo : {mecanismo}")
        self.stdout.write(f"Filas     : {len(comisiones)}")
        self.stdout.write("")

        for comision in comisiones:
            tasa = str(comision.tasa) if comision.tasa else "SIN TASA LEGIBLE"
            marca = "" if comision.tasa and comision.tasa.exacta_en_bps else "  (redondeada)"
            self.stdout.write(
                f"  {str(comision.alcance):14} {comision.clave:>8}  "
                f"{comision.nombre[:34]:34} {tasa}{marca}"
            )

        if mecanismo == MECANISMO_SIN_DETERMINAR:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "El mecanismo de comision sigue SIN DETERMINAR, asi que el "
                    "motor de precios reportara margen desconocido. Se resuelve "
                    "midiendo el saldo antes y despues de una recarga, no "
                    "leyendo un porcentaje."
                )
            )

        if seco:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Marcha en seco: no se escribio nada."))
            return

        nuevas, actualizadas = self._guardar(slug, ambiente, mecanismo, comisiones)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Nuevas      : {nuevas}"))
        self.stdout.write(f"Actualizadas: {actualizadas}")

    @transaction.atomic
    def _guardar(
        self, slug: str, ambiente: str, mecanismo: str, comisiones: list[Any]
    ) -> tuple[int, int]:
        ahora = timezone.now()
        nuevas = 0
        actualizadas = 0

        for comision in comisiones:
            alcance = str(comision.alcance)
            if alcance not in AlcanceComision.values:
                alcance = AlcanceComision.INDETERMINADO

            _, creada = ProviderCommission.objects.update_or_create(
                provider_slug=slug,
                environment=ambiente,
                alcance=alcance,
                clave=str(comision.clave)[:128],
                defaults={
                    "nombre": (comision.nombre or "")[:160],
                    "categoria": (comision.categoria or "")[:120],
                    "tasa_bps": comision.tasa.bps if comision.tasa else None,
                    "tasa_texto": (comision.tasa.crudo if comision.tasa else "")[:32],
                    "exacta_en_bps": bool(
                        comision.tasa.exacta_en_bps if comision.tasa else True
                    ),
                    "mecanismo": mecanismo[:32],
                    "raw": dict(comision.raw),
                    "observada_en": ahora,
                },
            )
            if creada:
                nuevas += 1
            else:
                actualizadas += 1

        return nuevas, actualizadas
