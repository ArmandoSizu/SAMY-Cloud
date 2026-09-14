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
        # Los candados, antes que nada y sin tocar la red.
        #
        # Apuntar a produccion y poder gastar en produccion son dos cosas
        # distintas, y desde que el host dice ``api.linn.mx`` la primera
        # pregunta de quien lee esto es la segunda. Ponerlo aqui arriba evita
        # que alguien deduzca "estamos en produccion, entonces ya vende".
        reporte["candados"] = self._candados(proveedor)

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

        # --- companias por endpoint dedicado -------------------------------
        reporte["companias_tae"] = self._intentar(
            lambda: self._companias(proveedor.companias_tae())
        )
        reporte["companias_virtuales"] = self._intentar(
            lambda: self._companias(proveedor.companias_virtuales())
        )
        reporte["discrepancias_entre_endpoints"] = self._discrepancias(reporte)

        self._imprimir(reporte, como_json)

    # -- secciones ---------------------------------------------------------

    def _candados(self, proveedor: Any) -> dict[str, Any]:
        """Que impide hoy que este proveedor gaste dinero. Sin llamar a nadie.

        Son tres cerrojos independientes y basta uno para que no se venda. Se
        reportan los tres por separado, y no un "no vende" a secas, porque
        abrir uno creyendo que era el unico es precisamente como se llega a
        vender sin querer.
        """
        from django.conf import settings

        from samy_common.providers.environment import (
            ambiente_actual,
            modo_esperado,
            verificar_ambiente,
        )

        try:
            ambiente = str(ambiente_actual())
            esperado = str(modo_esperado(ambiente_actual()))
        except Exception as exc:  # ambiente no reconocido: tambien es un candado
            ambiente = f"ILEGIBLE ({type(exc).__name__})"
            esperado = "-"

        try:
            verificar_ambiente(
                provider_slug=proveedor.slug, provider_mode=proveedor.mode
            )
            ambiente_ok = True
            ambiente_detalle = (
                f"ENVIRONMENT={ambiente} concuerda con el modo {proveedor.mode}."
            )
        except Exception as exc:
            ambiente_ok = False
            ambiente_detalle = str(exc)[:240]

        candados = {
            "ambiente_del_servicio": {
                "abierto": ambiente_ok,
                "ENVIRONMENT": ambiente,
                "modo_del_proveedor": str(proveedor.mode),
                "modo_exigido_por_el_ambiente": esperado,
                "detalle": ambiente_detalle,
            },
            "autorizacion_para_mover_dinero": {
                "abierto": bool(proveedor.config.permitir_operaciones_reales),
                "LINNTAE_ENABLED": bool(settings.LINNTAE_ENABLED),
                "ALLOW_REAL_PROVIDER_TRANSACTIONS": bool(
                    settings.ALLOW_REAL_PROVIDER_TRANSACTIONS
                ),
            },
            "type_balance_confirmado": {
                "abierto": proveedor.config.type_balance is not None,
                "valor": proveedor.config.type_balance,
                "detalle": (
                    "LINNTAE_TYPE_BALANCE vacio: no sabemos de que bolsa se "
                    "descuenta una compra, y adivinarlo es cobrarle al cliente "
                    "antes de saber si la recarga se puede mandar."
                    if proveedor.config.type_balance is None
                    else "Confirmado."
                ),
            },
        }
        candados["puede_vender"] = all(c["abierto"] for c in candados.values())
        return candados

    def _companias(self, companias: list[Any]) -> dict[str, Any]:
        return {
            "total": len(companias),
            "ofertas": sum(len(c.ofertas) for c in companias),
            "operadores": [
                {
                    "idOperator": c.id_operator,
                    "nombre": c.nombre,
                    "ofertas": len(c.ofertas),
                }
                for c in companias
            ],
        }

    def _discrepancias(self, reporte: dict[str, Any]) -> list[str]:
        """Compara ``syncProducts`` contra los dos endpoints por seccion.

        Linntae publica el mismo catalogo por tres puertas y en DEMO las tres
        no coincidian: un operador con 13 ofertas por una puerta y 12 por
        otra. Importa porque el emparejamiento comercial lee UNA de ellas: si
        la oferta que se vende solo existe en la puerta que no leemos, el
        producto aparece vendible y la compra falla en el unico momento en que
        ya se cobro. Comprobarlo a mano una vez es facil y se olvida; por eso
        vive aqui.
        """
        catalogo = (reporte.get("catalogo") or {}).get("datos") or {}
        if not catalogo:
            return []

        por_sync: dict[str, tuple[str, int]] = {
            str(o.get("idOperator")): (str(o.get("nombre")), int(o.get("ofertas") or 0))
            for o in catalogo.get("operadores") or []
        }

        avisos: list[str] = []
        for clave, titulo in (
            ("companias_tae", "taeCompanies"),
            ("companias_virtuales", "taeVirtualCompanies"),
        ):
            bloque = (reporte.get(clave) or {}).get("datos") or {}
            for operador in bloque.get("operadores") or []:
                id_op = str(operador.get("idOperator"))
                nombre = str(operador.get("nombre"))
                aqui = int(operador.get("ofertas") or 0)
                if id_op not in por_sync:
                    avisos.append(
                        f"{nombre} (idOperator {id_op}) aparece en {titulo} y "
                        "NO en syncProducts."
                    )
                    continue
                _, alla = por_sync[id_op]
                if aqui != alla:
                    avisos.append(
                        f"{nombre} (idOperator {id_op}): syncProducts declara "
                        f"{alla} ofertas y {titulo} declara {aqui}."
                    )
        return avisos

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

        self._candados_impresos(reporte.get("candados"))

        self._seccion("SALDO", reporte.get("saldo"))
        self._seccion("ESQUEMA", reporte.get("esquema"))
        self._seccion("COMISIONES", reporte.get("comisiones"), resumen_comisiones=True)
        self._seccion("CATALOGO", reporte.get("catalogo"), resumen_catalogo=True)
        self._seccion(
            "COMPANIAS (taeCompanies)", reporte.get("companias_tae"), resumen_companias=True
        )
        self._seccion(
            "COMPANIAS (taeVirtualCompanies)",
            reporte.get("companias_virtuales"),
            resumen_companias=True,
        )

        avisos = reporte.get("discrepancias_entre_endpoints") or []
        self.stdout.write("")
        self.stdout.write("-- DISCREPANCIAS ENTRE ENDPOINTS " + "-" * 24)
        if not avisos:
            self.stdout.write(self.style.SUCCESS("  Ninguna: las tres puertas coinciden."))
        for aviso in avisos:
            self.stdout.write(self.style.WARNING(f"  ! {aviso}"))

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

    def _candados_impresos(self, candados: dict[str, Any] | None) -> None:
        if not candados:
            return
        self.stdout.write("")
        self.stdout.write("-- CANDADOS (que impide vender hoy) " + "-" * 21)
        etiquetas = {
            "ambiente_del_servicio": "Ambiente del servicio",
            "autorizacion_para_mover_dinero": "Autorizacion para mover dinero",
            "type_balance_confirmado": "typeBalance confirmado",
        }
        for clave, etiqueta in etiquetas.items():
            bloque = candados.get(clave) or {}
            abierto = bool(bloque.get("abierto"))
            # ABIERTO es la palabra alarmante aqui: un candado abierto es un
            # permiso concedido, no una prueba que paso.
            marca = (
                self.style.WARNING("ABIERTO")
                if abierto
                else self.style.SUCCESS("CERRADO")
            )
            self.stdout.write(f"  {etiqueta:32} {marca}")
            detalle = bloque.get("detalle")
            if detalle and not abierto:
                self.stdout.write(f"  {'':32} {str(detalle)[:110]}")

        puede = bool(candados.get("puede_vender"))
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING("  => PUEDE VENDER: los tres candados estan abiertos.")
            if puede
            else self.style.SUCCESS(
                "  => NO PUEDE VENDER. Basta un candado cerrado, y hay al menos uno."
            )
        )

    def _seccion(
        self,
        titulo: str,
        bloque: dict[str, Any] | None,
        *,
        resumen_comisiones: bool = False,
        resumen_catalogo: bool = False,
        resumen_companias: bool = False,
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

        if resumen_companias:
            self.stdout.write(
                f"  Operadores: {datos.get('total')}  Ofertas: {datos.get('ofertas')}"
            )
            for operador in datos.get("operadores") or []:
                self.stdout.write(
                    f"    {str(operador.get('idOperator')):>6}  "
                    f"{str(operador.get('nombre'))[:34]:34} "
                    f"{operador.get('ofertas')} ofertas"
                )
            return

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
