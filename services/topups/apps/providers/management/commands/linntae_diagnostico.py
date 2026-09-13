"""Diagnostico de Linntae. **Solo lectura. No mueve un peso.**

    python manage.py linntae_diagnostico
    python manage.py linntae_diagnostico --operador TELCEL
    python manage.py linntae_diagnostico --json

Hace, en este orden, las cinco cosas que se pueden hacer sin dinero:

    1. autenticar;
    2. consultar saldo;
    3. leer el esquema comercial de la cuenta;
    4. leer las comisiones realmente asignadas;
    5. leer el catalogo y localizar al operador que se pida.

Y no hace nada mas. No escribe en la base de datos, no crea mappings, no
habilita productos y **no llama a ningun endpoint de compra**. Se puede correr
tantas veces como haga falta sin consecuencias.

POR QUE ESTE COMANDO EXISTE, Y NO SE USA ``importar_catalogo_proveedor``
-----------------------------------------------------------------------

Ese comando escribe, y exige que el proveedor este READY. Linntae no puede
estar READY hasta que ``LINNTAE_TYPE_BALANCE`` este confirmado, porque un
producto vendible cuyo ``typeBalance`` falta produciria exactamente la
secuencia prohibida: cobrar al cliente y despues descubrir que la recarga no
se puede mandar.

Pero hay que poder MIRAR el catalogo y las comisiones antes de decidir nada.
Eso es lo que hace este comando: mirar.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.providers.linntae.parseo import RespuestaIlegible
from apps.providers.registry import get_provider
from samy_common.providers.exceptions import ProviderError


class Command(BaseCommand):
    help = "Diagnostico de solo lectura de Linntae: token, saldo, catalogo y comisiones."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--operador",
            default="TELCEL",
            help="Nombre del operador a destacar en el reporte (por omision TELCEL).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="como_json",
            help="Salida en JSON, para pegarla en un reporte.",
        )
        parser.add_argument(
            "--max-ofertas",
            type=int,
            default=40,
            help="Cuantas ofertas del operador destacado se listan.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        operador_buscado = str(options["operador"]).strip().upper()
        como_json = bool(options["como_json"])
        max_ofertas = int(options["max_ofertas"])

        proveedor = get_provider("linntae")
        # La configuracion se registra ANTES de cualquier salida temprana.
        #
        # Antes se ponia al final, y en el camino de "faltan credenciales" el
        # reporte salia diciendo "extraComision: None" cuando en realidad
        # estaba en 0. Un diagnostico que se contradice a si mismo es peor
        # que uno incompleto: manda a buscar un problema que no existe.
        reporte: dict[str, Any] = {
            "proveedor": "linntae",
            "modo": str(proveedor.mode),
            "ambiente": proveedor.config.ambiente_normalizado,
            "host": proveedor.config.host,
            "type_balance_configurado": proveedor.config.type_balance,
            "extra_comision_configurada": proveedor.config.extra_comision,
            "operaciones_reales_habilitadas": bool(
                proveedor.config.permitir_operaciones_reales
            ),
        }

        # --- ambiente ------------------------------------------------------
        problemas = proveedor.config.problemas_de_ambiente()
        reporte["problemas_de_ambiente"] = list(problemas)
        if problemas:
            # Se detiene aqui: si la pareja ambiente/URL no es coherente, no
            # hay diagnostico que hacer, hay configuracion que arreglar.
            self._imprimir(reporte, como_json)
            raise CommandError(
                "La configuracion de ambiente de Linntae no es coherente. No se "
                "hace ninguna llamada."
            )

        if not proveedor.config.credenciales_completas:
            reporte["credenciales"] = "INCOMPLETAS"
            self._imprimir(reporte, como_json)
            raise CommandError(
                "Faltan LINNTAE_USERNAME o LINNTAE_PASSWORD en el .env. No se "
                "piden por chat ni se escriben en el repositorio."
            )
        reporte["credenciales"] = "COMPLETAS"

        # --- salud (autentica y consulta saldo de verdad) ------------------
        salud = proveedor.check_health()
        reporte["salud"] = {
            "estado": str(salud.status),
            "detalle": salud.detail,
            "falta": list(salud.missing_requirements),
            "latencia_ms": salud.latency_ms,
        }

        # --- saldo ---------------------------------------------------------
        reporte["saldo"] = self._intentar(lambda: self._saldos(proveedor))

        # --- esquema -------------------------------------------------------
        reporte["esquema"] = self._intentar(lambda: self._esquema(proveedor))

        # --- comisiones ----------------------------------------------------
        reporte["comisiones"] = self._intentar(lambda: self._comisiones(proveedor))

        # --- catalogo ------------------------------------------------------
        reporte["catalogo"] = self._intentar(
            lambda: self._catalogo(proveedor, operador_buscado, max_ofertas)
        )

        self._imprimir(reporte, como_json)

    # -- secciones ---------------------------------------------------------

    def _saldos(self, proveedor: Any) -> dict[str, Any]:
        saldos = proveedor.saldos()
        return {
            "plataforma": str(saldos.plataforma) if saldos.plataforma else None,
            "plataforma_cents": saldos.plataforma.cents if saldos.plataforma else None,
            "comision": str(saldos.comision) if saldos.comision else None,
            "servicios": str(saldos.servicios) if saldos.servicios else None,
            "crudos": saldos.crudos,
        }

    def _esquema(self, proveedor: Any) -> dict[str, Any]:
        esquema = proveedor.esquema()
        return {"id": esquema.id, "nombre": esquema.nombre}

    def _comisiones(self, proveedor: Any) -> dict[str, Any]:
        comisiones = proveedor.comisiones()
        return {
            "total": len(comisiones),
            "inexactas_en_bps": [
                c.nombre for c in comisiones if c.tasa and not c.tasa.exacta_en_bps
            ],
            "sin_tasa_legible": [c.nombre for c in comisiones if c.tasa is None],
            "filas": [
                {
                    "alcance": str(c.alcance),
                    "clave": c.clave,
                    "nombre": c.nombre,
                    "categoria": c.categoria,
                    "tasa": str(c.tasa) if c.tasa else None,
                    "bps": c.tasa.bps if c.tasa else None,
                }
                for c in comisiones
            ],
        }

    def _catalogo(
        self, proveedor: Any, operador: str, max_ofertas: int
    ) -> dict[str, Any]:
        catalogo = proveedor.catalogo_crudo()

        destacado = [
            c for c in catalogo.companias if c.nombre.strip().upper() == operador
        ]
        # Si no hay coincidencia exacta, se reporta lo que SI hay. No se
        # empareja por parecido: "Telcel" y "Telcel Sin Limites" son dos
        # operadores distintos en su catalogo y elegir uno por contener al
        # otro es como se manda el producto equivocado.
        return {
            "companias": len(catalogo.companias),
            "ofertas": catalogo.total_ofertas,
            "productos_con_sku": len(catalogo.con_sku),
            "secciones_desconocidas": list(catalogo.secciones_desconocidas),
            "operadores": [
                {
                    "idOperator": c.id_operator,
                    "nombre": c.nombre,
                    "seccion": c.seccion,
                    "ofertas": len(c.ofertas),
                }
                for c in catalogo.companias
            ],
            "operador_buscado": operador,
            "encontrado": bool(destacado),
            "ofertas_del_operador": [
                {
                    "idOffer": o.id_offer,
                    "monto": str(o.monto) if o.monto else None,
                    "monto_cents": o.monto.cents if o.monto else None,
                    "descripcion": o.descripcion,
                    "categoria": o.categoria,
                }
                for c in destacado
                for o in c.ofertas
            ][:max_ofertas],
        }

    # -- utilidades --------------------------------------------------------

    def _intentar(self, funcion: Any) -> dict[str, Any]:
        """Corre una seccion y captura su fallo sin abortar el diagnostico.

        Un diagnostico que se detiene en el primer fallo obliga a correrlo
        cinco veces para ver cinco problemas. Aqui cada seccion reporta lo
        suyo y el resto sigue.
        """
        try:
            return {"ok": True, "datos": funcion()}
        except (ProviderError, RespuestaIlegible) as exc:
            return {"ok": False, "error": str(exc)[:400], "tipo": type(exc).__name__}

    def _imprimir(self, reporte: dict[str, Any], como_json: bool) -> None:
        if como_json:
            self.stdout.write(json.dumps(reporte, indent=2, ensure_ascii=False))
            return

        self.stdout.write("")
        self.stdout.write("LINNTAE - DIAGNOSTICO DE SOLO LECTURA")
        self.stdout.write("=" * 60)
        self.stdout.write(f"Ambiente     : {reporte.get('ambiente')}")
        self.stdout.write(f"Modo         : {reporte.get('modo')}")
        self.stdout.write(f"Host         : {reporte.get('host')}")
        self.stdout.write(f"Credenciales : {reporte.get('credenciales')}")

        for problema in reporte.get("problemas_de_ambiente") or []:
            self.stdout.write(self.style.ERROR(f"  ! {problema}"))

        salud = reporte.get("salud") or {}
        if salud:
            self.stdout.write("")
            self.stdout.write(f"Salud        : {salud.get('estado')}")
            self.stdout.write(f"  {salud.get('detalle')}")
            for falta in salud.get("falta") or []:
                self.stdout.write(self.style.WARNING(f"  falta: {falta}"))

        self._seccion("SALDO", reporte.get("saldo"))
        self._seccion("ESQUEMA", reporte.get("esquema"))
        self._seccion("COMISIONES", reporte.get("comisiones"), resumen_comisiones=True)
        self._seccion("CATALOGO", reporte.get("catalogo"), resumen_catalogo=True)

        self.stdout.write("")
        self.stdout.write(
            "typeBalance configurado : "
            f"{reporte.get('type_balance_configurado')}"
        )
        self.stdout.write(
            "extraComision configurada: "
            f"{reporte.get('extra_comision_configurada')}"
        )
        habilitado = reporte.get("operaciones_reales_habilitadas")
        estilo = self.style.WARNING if habilitado else self.style.SUCCESS
        self.stdout.write(
            estilo(f"Operaciones reales      : {'HABILITADAS' if habilitado else 'BLOQUEADAS'}")
        )
        self.stdout.write("")
        self.stdout.write("Este comando no escribio nada y no llamo a ningun endpoint de compra.")
        self.stdout.write("")

    def _seccion(
        self,
        titulo: str,
        bloque: dict[str, Any] | None,
        *,
        resumen_comisiones: bool = False,
        resumen_catalogo: bool = False,
    ) -> None:
        self.stdout.write("")
        self.stdout.write(f"-- {titulo} " + "-" * max(0, 56 - len(titulo)))
        if not bloque:
            self.stdout.write("  (no se ejecuto)")
            return
        if not bloque.get("ok"):
            self.stdout.write(self.style.ERROR(f"  FALLO: {bloque.get('error')}"))
            return

        datos = bloque.get("datos") or {}

        if resumen_comisiones:
            self.stdout.write(f"  Filas: {datos.get('total')}")
            for fila in datos.get("filas") or []:
                self.stdout.write(
                    f"    {str(fila.get('alcance')):14} {str(fila.get('clave')):10} "
                    f"{str(fila.get('nombre'))[:32]:32} {str(fila.get('tasa'))}"
                )
            for nombre in datos.get("sin_tasa_legible") or []:
                self.stdout.write(self.style.WARNING(f"    sin tasa legible: {nombre}"))
            for nombre in datos.get("inexactas_en_bps") or []:
                self.stdout.write(
                    self.style.WARNING(f"    tasa redondeada a puntos base: {nombre}")
                )
            return

        if resumen_catalogo:
            self.stdout.write(
                f"  Companias: {datos.get('companias')}  "
                f"Ofertas: {datos.get('ofertas')}  "
                f"Con SKU: {datos.get('productos_con_sku')}"
            )
            for seccion in datos.get("secciones_desconocidas") or []:
                self.stdout.write(
                    self.style.WARNING(f"    seccion no reconocida: {seccion}")
                )
            for operador in datos.get("operadores") or []:
                self.stdout.write(
                    f"    {operador['idOperator']:>5}  {operador['nombre'][:24]:24} "
                    f"{operador['seccion'][:18]:18} {operador['ofertas']:>4} ofertas"
                )
            self.stdout.write("")
            encontrado = datos.get("encontrado")
            etiqueta = datos.get("operador_buscado")
            if encontrado:
                self.stdout.write(f"  Ofertas de {etiqueta}:")
                for oferta in datos.get("ofertas_del_operador") or []:
                    self.stdout.write(
                        f"    idOffer {oferta['idOffer']:>6}  "
                        f"{str(oferta['monto'] or 'monto libre'):>16}  "
                        f"{(oferta['categoria'] or '-')[:18]:18} "
                        f"{oferta['descripcion'][:40]}"
                    )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {etiqueta} NO aparece con ese nombre exacto en el "
                        "catalogo. No se empareja por parecido."
                    )
                )
            return

        for llave, valor in datos.items():
            self.stdout.write(f"  {llave}: {valor}")
