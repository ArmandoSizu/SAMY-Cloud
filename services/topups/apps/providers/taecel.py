"""Adaptador de TAECEL (distribuidor mexicano de tiempo aire).

ESTADO (13 de septiembre de 2026): **CUENTA REGISTRADA, API PENDIENTE.**

    TAECEL_REGISTERED   = TRUE   (cuenta creada y activa)
    TAECEL_API_REQUESTED = PENDING (levantamiento tecnologico en tramite)

Ver ``docs/readiness-produccion.md`` para el estado consolidado.

PROCEDENCIA DE CADA DATO DE ESTE ARCHIVO
----------------------------------------

Esta tabla existe porque la diferencia entre "lo verifique" y "me acorde"
es la diferencia entre una recarga que se aplica y dinero perdido. Nada de
lo marcado ``SIN CONFIRMAR`` se ejecuta mientras no lo confirme una persona.

==========================  ==========================================
Dato                        Procedencia
==========================  ==========================================
Key + NIP como credencial   VERIFICADO. TAECEL las entrega por correo
                            al dar de alta la cuenta y las muestra en
                            su portal (taecel.com/app/, seccion de
                            administracion). Corroborado por un
                            integrador externo (eleventa.com).
Modelo de saldo prepagado   VERIFICADO. Folleto de integrador.
La API es REST              VERIFICADO. Su pagina exige "un programador
                            con experiencia en API REST".
URL base                    **NO PUBLICADA.** Llega con las credenciales.
                            Sin valor por omision en este archivo.
RequestTXN / StatusTXN      **SIN CONFIRMAR.** Los nombres y sus campos
y sus campos                (Key, NIP, Producto, Referencia, Monto /
                            Key, NIP, transID) los aporto Sizu. NO
                            aparecen en ninguna fuente oficial de
                            TAECEL consultada el 13/09/2026.
Forma de la respuesta       **SIN CONFIRMAR.** TAECEL no publica campos
                            ni codigos de error. El parser de este
                            archivo NUNCA deduce exito: lo que no
                            reconoce va a conciliacion.
Idempotencia / reversos     **NO VERIFICADO.** No se asume ninguna.
Comisiones                  **NO PUBLICADAS.** "Preguntanos por el
                            porcentaje" (folleto).
==========================  ==========================================

Se revisaron el 13/09/2026, sin encontrar un solo endpoint publicado:
``taecel.com/portal/integracion-web-services``,
``taecel.com/portal/vender-recargas-telcel-por-webservice-cadenas-comerciales-mayoristas``
y ``cdn.taecel.com/src/web/taecel/descargas/integrador-api-taecel.pdf``.

LAS TRES LLAVES QUE ABREN ESTE ADAPTADOR
----------------------------------------

``check_health()`` devuelve ``PENDING_CONTRACT`` y toda operacion levanta
``ProviderNotConfigured`` mientras falte cualquiera de estas tres:

1. ``TAECEL_BASE_URL``  — la URL que TAECEL entrega con las credenciales.
2. ``TAECEL_KEY`` y ``TAECEL_NIP`` — las credenciales.
3. ``TAECEL_CONTRACT_VERIFIED=True`` — una persona leyo la documentacion
   real de TAECEL y confirmo que las rutas y los campos de abajo coinciden.

La tercera es la que importa. Las dos primeras podrian estar puestas y el
contrato seguir siendo una suposicion; esa bandera es la firma humana de
que ya no lo es. Se activa DESPUES de leer el manual, no antes.

Cuando lleguen las credenciales de prueba el trabajo es: poner las cuatro
variables, corregir los nombres de campo de ``CAMPOS_*`` si el manual dice
otra cosa, y encender la bandera. No hay arquitectura que rehacer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import httpx
import structlog

from apps.providers.base import (
    CatalogProduct,
    TopupProvider,
    TopupRequest,
    TopupResult,
    TopupStatus,
)
from apps.providers.registry import topup_registry
from samy_common.money import Money
from samy_common.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderMode,
    ProviderStatus,
)
from samy_common.providers.exceptions import (
    ProviderIndeterminateError,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.saldo import SaldoProveedor

log = structlog.get_logger("provider.taecel")

CURRENCY_MX: Final[str] = "MXN"

# ---------------------------------------------------------------------------
# Rutas de operacion — SIN CONFIRMAR
# ---------------------------------------------------------------------------
# Estos son los nombres de operacion que aporto Sizu. No estan publicados por
# TAECEL. Son valores por omision SOBREESCRIBIBLES por entorno justamente
# porque son suposiciones: si el manual real dice "/api/RequestTXN" o
# "?op=RequestTXN", se corrige con una variable y sin tocar codigo.
#
# Nada se envia a ninguna de las dos mientras TAECEL_CONTRACT_VERIFIED sea
# False, asi que una ruta equivocada aqui no puede provocar una llamada real.
RUTA_REQUEST_TXN_POR_OMISION: Final[str] = "RequestTXN"
RUTA_STATUS_TXN_POR_OMISION: Final[str] = "StatusTXN"

#: Campos de la peticion de recarga. SIN CONFIRMAR (ver tabla del docstring).
#:
#: Se declaran como constantes y no incrustados en el codigo para que
#: corregirlos contra el manual real sea una linea, no una caceria.
CAMPO_KEY: Final[str] = "Key"
CAMPO_NIP: Final[str] = "NIP"
CAMPO_PRODUCTO: Final[str] = "Producto"
CAMPO_REFERENCIA: Final[str] = "Referencia"
CAMPO_MONTO: Final[str] = "Monto"
CAMPO_TRANS_ID: Final[str] = "transID"

#: Nombres bajo los que se busca el folio de TAECEL en su respuesta. Se
#: prueban en orden. SIN CONFIRMAR.
LLAVES_FOLIO: Final[tuple[str, ...]] = ("transID", "TransID", "transid", "folio")

#: Marcadores de exito que se aceptan. **Lista deliberadamente corta.**
#:
#: Todo lo que no este aqui se traduce a UNKNOWN, no a FAILED y jamas a
#: SUCCEEDED. Un desconocido va a conciliacion; darlo por fallido cuando la
#: recarga si se aplico regala el saldo, y darlo por bueno cuando no se
#: aplico cobra un servicio que nadie recibio.
ESTADOS_EXITO: Final[frozenset[str]] = frozenset(
    {"1", "TRUE", "OK", "EXITO", "EXITOSA", "SUCCESS", "SUCCESSFUL", "APROBADA"}
)
ESTADOS_FALLO: Final[frozenset[str]] = frozenset(
    {"0", "FALSE", "ERROR", "FALLO", "FALLIDA", "FAILED", "RECHAZADA", "DECLINADA"}
)
ESTADOS_PENDIENTE: Final[frozenset[str]] = frozenset(
    {"PENDIENTE", "PENDING", "EN PROCESO", "PROCESSING", "EN_PROCESO"}
)

#: Llaves bajo las que se busca el estado en la respuesta. SIN CONFIRMAR.
LLAVES_ESTADO: Final[tuple[str, ...]] = ("success", "Success", "estado", "status", "Status")

#: Llaves bajo las que se busca el saldo disponible. SIN CONFIRMAR.
LLAVES_SALDO: Final[tuple[str, ...]] = ("saldo", "Saldo", "balance", "Balance", "disponible")

#: TAECEL es REST (verificado), pero no publica si espera JSON o formulario.
#: Los web services mexicanos de este tipo suelen recibir formulario. SIN
#: CONFIRMAR: si el manual dice JSON, esta constante es la unica linea a
#: cambiar. Nada se envia mientras el contrato no este confirmado.
ENVIO_FORM_ENCODED: Final[bool] = True

#: Lo que falta para operar. Se muestra tal cual en el panel de plataforma.
PENDING_REQUIREMENTS: tuple[str, ...] = (
    "Levantamiento tecnologico enviado a cc@taecel.com y aprobado",
    "Documentacion oficial del web service (TAECEL no la publica)",
    "URL base del web service (TAECEL_BASE_URL)",
    "Credenciales TAECEL_KEY y TAECEL_NIP",
    "Confirmacion humana del contrato HTTP (TAECEL_CONTRACT_VERIFIED)",
    "Saldo prepagado fondeado en la cuenta",
)


@dataclass(frozen=True, slots=True)
class TaecelConfig:
    """Configuracion de TAECEL. Todo llega por entorno, nada vive en el repo."""

    #: URL del web service. **Sin valor por omision a proposito.** TAECEL no
    #: la publica; la entrega con las credenciales. Un valor inventado aqui
    #: seria una llamada a un servidor que no es el de TAECEL.
    base_url: str = ""
    key: str = ""
    nip: str = ""
    #: Firma humana de que la documentacion real fue leida y las rutas y
    #: campos de este archivo coinciden con ella. Sin esto no se opera.
    contract_verified: bool = False
    #: Sobreescribibles porque las rutas por omision son suposiciones.
    path_request_txn: str = RUTA_REQUEST_TXN_POR_OMISION
    path_status_txn: str = RUTA_STATUS_TXN_POR_OMISION
    #: Consulta de saldo. **Sin omision**: TAECEL no publica su nombre y sin
    #: saldo confirmado no se vende (ver ``_salud_con_saldo``).
    path_balance: str = ""
    #: Catalogo de productos del proveedor. Sin omision, por lo mismo.
    path_products: str = ""
    timeout_seconds: float = 25.0

    @property
    def credenciales_completas(self) -> bool:
        return bool(self.base_url and self.key and self.nip)


@topup_registry.register
class TaecelProvider(TopupProvider):
    """Distribuidor mexicano de tiempo aire. Cuenta activa, API en tramite."""

    slug = "taecel"
    display_name = "TAECEL"
    capabilities = frozenset(
        {ProviderCapability.AIRTIME_TOPUP, ProviderCapability.CATALOG_SYNC}
    )
    required_settings = ("TAECEL_BASE_URL", "TAECEL_KEY", "TAECEL_NIP")
    requires_commercial_contract = True
    documentation_url = "https://taecel.com/portal/integracion-web-services"

    # -- salud -------------------------------------------------------------

    def check_health(self) -> ProviderHealth:
        """Tres llaves. Falta una y no se opera.

        No hay rama de este metodo que devuelva READY sin haber hablado de
        verdad con TAECEL: la ultima comprueba saldo contra su web service.
        """
        faltantes = [
            nombre
            for nombre, valor in (
                ("TAECEL_BASE_URL", self.config.base_url),
                ("TAECEL_KEY", self.config.key),
                ("TAECEL_NIP", self.config.nip),
            )
            if not valor
        ]

        if faltantes:
            return ProviderHealth(
                status=ProviderStatus.PENDING_CONTRACT,
                detail=(
                    "Cuenta TAECEL registrada y activa, pero el acceso API esta "
                    "en tramite: falta el levantamiento tecnologico aprobado y "
                    f"las credenciales. Sin definir: {', '.join(faltantes)}. "
                    "TAECEL no publica su documentacion: la entrega con las "
                    "credenciales de prueba. Ver docs/readiness-produccion.md."
                ),
                missing_requirements=PENDING_REQUIREMENTS,
            )

        if not self.config.contract_verified:
            # Este es el caso peligroso y por eso tiene su propia rama: hay
            # credenciales, asi que una llamada AQUI seria una llamada real
            # contra el sistema de TAECEL usando un contrato que nadie ha
            # leido. Se corta antes de tocar la red.
            return ProviderHealth(
                status=ProviderStatus.PENDING_CONTRACT,
                detail=(
                    "Hay credenciales de TAECEL, pero el contrato HTTP sigue sin "
                    "confirmar. Las rutas (RequestTXN / StatusTXN) y los campos "
                    "de este adaptador NO provienen de documentacion oficial de "
                    "TAECEL. Lee el manual que acompana a las credenciales, "
                    "corrige lo que difiera y entonces pon "
                    "TAECEL_CONTRACT_VERIFIED=True."
                ),
                missing_requirements=(
                    "Documentacion oficial del web service de TAECEL",
                    "Cotejar rutas y campos de taecel.py contra ese manual",
                    "TAECEL_CONTRACT_VERIFIED=True",
                ),
            )

        return self._salud_con_saldo()

    def _salud_con_saldo(self) -> ProviderHealth:
        """Ultima puerta: saldo real. TAECEL es prepago.

        Un proveedor prepago sin saldo NO puede recargar. Reportarlo READY
        produce el peor fallo posible en este sistema: la orden ya esta
        PAGADA, el cliente ya dio su dinero, y la recarga muere. Por eso
        "configurado" no es "operativo" hasta que se ve el saldo.

        Si no hay endpoint de saldo confirmado, la respuesta honesta no es
        READY sino DEGRADED: no sabemos si hay fondos, y no saberlo es razon
        suficiente para no vender.
        """
        if not self.config.path_balance:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    "Contrato confirmado, pero no hay endpoint de consulta de "
                    "saldo configurado, asi que no se puede afirmar que la "
                    "cuenta tenga fondos. TAECEL es prepago: vender sin saber "
                    "el saldo arriesga dejar una orden pagada sin recarga. "
                    "Define TAECEL_PATH_BALANCE con la ruta que indique su "
                    "manual."
                ),
                missing_requirements=(
                    "TAECEL_PATH_BALANCE (ruta de consulta de saldo)",
                ),
            )

        inicio = time.perf_counter()
        try:
            respuesta = self._llamar(self.config.path_balance, {})
        except ProviderPermanentError as exc:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=f"TAECEL rechazo las credenciales: {exc.message}",
                missing_requirements=("Credenciales validas de TAECEL",),
            )
        except (httpx.HTTPError, ProviderTransientError) as exc:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=f"No se pudo contactar a TAECEL: {exc}",
            )

        saldo = self._leer_saldo(respuesta)
        if saldo is None:
            # No se encontro el saldo en la respuesta. Puede ser que el nombre
            # del campo no sea el que suponemos. En cualquier caso: no se
            # afirma READY sobre una respuesta que no se entendio.
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    "TAECEL respondio, pero no se reconocio el saldo en su "
                    "respuesta. Revisa los nombres de campo contra su manual "
                    f"(se buscaron: {', '.join(LLAVES_SALDO)})."
                ),
                missing_requirements=("Nombre real del campo de saldo",),
            )

        if saldo <= 0:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    f"TAECEL esta configurado pero el saldo es {saldo} "
                    f"{CURRENCY_MX}. Hay que fondear la cuenta antes de vender."
                ),
                missing_requirements=("Saldo prepagado en la cuenta de TAECEL",),
            )

        return ProviderHealth(
            status=ProviderStatus.READY,
            detail=(
                f"TAECEL operativo en modo {self.mode}. "
                f"Saldo: {saldo} {CURRENCY_MX}."
            ),
            latency_ms=int((time.perf_counter() - inicio) * 1000),
        )

    # -- saldo -------------------------------------------------------------

    def saldo_disponible(self) -> SaldoProveedor:
        """Saldo prepagado en TAECEL, en MXN.

        Mientras el contrato HTTP no este confirmado o falte la ruta de
        saldo, devuelve "no lo se" -- no cero. En produccion eso bloquea la
        venta, que es lo correcto: TAECEL es prepago y vender sin poder
        verificar fondos deja ordenes cobradas sin recarga.
        """
        if not (self.config.credenciales_completas and self.config.contract_verified):
            return SaldoProveedor(
                disponible=None,
                detalle="TAECEL sin credenciales o con el contrato sin confirmar.",
            )
        if not self.config.path_balance:
            return SaldoProveedor(
                disponible=None, detalle="Falta TAECEL_PATH_BALANCE."
            )

        try:
            datos = self._llamar(self.config.path_balance, {})
        except (httpx.HTTPError, ProviderTransientError, ProviderPermanentError) as exc:
            return SaldoProveedor(
                disponible=None, detalle=f"No se pudo consultar a TAECEL: {exc}"
            )

        crudo = self._leer_saldo(datos)
        if crudo is None:
            return SaldoProveedor(
                disponible=None,
                detalle="TAECEL respondio pero no se reconocio el saldo.",
            )

        # _leer_saldo devuelve float por venir de texto con separadores. Se
        # convierte a centavos por la via de Decimal, nunca multiplicando el
        # float por 100: ahi es donde aparecen los centavos fantasma.
        try:
            disponible = Money.parse(Decimal(str(crudo)), CURRENCY_MX)
        except (ValueError, ArithmeticError, TypeError):
            return SaldoProveedor(
                disponible=None, detalle=f"Saldo ilegible de TAECEL: {crudo!r}"
            )

        return SaldoProveedor(
            disponible=disponible, detalle=f"Saldo prepagado TAECEL ({self.mode})."
        )

    # -- catalogo ----------------------------------------------------------

    def fetch_catalog(self) -> list[CatalogProduct]:
        """Descarga el catalogo REAL de productos de TAECEL.

        Lo que devuelve este metodo NO es lo que el cajero ve. Es el catalogo
        TECNICO del proveedor: la lista de SKUs que TAECEL sabe ejecutar. El
        catalogo comercial (``apps.commercial``) es otra cosa y se mapea
        contra este por identidad exacta, nunca por precio.
        """
        self.ensure_ready()
        if not self.config.path_products:
            raise ProviderNotConfigured(
                provider=self.slug,
                message=(
                    "No hay ruta de catalogo configurada para TAECEL. Define "
                    "TAECEL_PATH_PRODUCTS con la que indique su manual. No se "
                    "inventa una lista de productos."
                ),
                missing_requirements=("TAECEL_PATH_PRODUCTS",),
                status=ProviderStatus.PENDING_CONTRACT,
            )

        datos = self._llamar(self.config.path_products, {})
        productos = self._parsear_catalogo(datos)
        log.info("taecel_catalog_fetched", productos=len(productos))
        return productos

    def _parsear_catalogo(self, datos: dict[str, Any]) -> list[CatalogProduct]:
        """Traduce la respuesta de TAECEL a ``CatalogProduct``.

        Conservador a proposito: un renglon que no trae identidad completa
        (codigo/SKU + operador + importe o rango) NO se convierte en producto.
        Un catalogo con huecos es un inconveniente; un producto con SKU
        equivocado es una recarga aplicada al paquete que no era.
        """
        filas = datos.get("data") if isinstance(datos.get("data"), list) else None
        if filas is None:
            filas = datos.get("productos") if isinstance(datos.get("productos"), list) else []

        productos: list[CatalogProduct] = []
        for fila in filas:
            if not isinstance(fila, dict):
                continue
            sku = str(
                fila.get("codigo") or fila.get("Codigo") or fila.get("sku") or ""
            ).strip()
            operador = str(
                fila.get("compania") or fila.get("Compania") or fila.get("operador") or ""
            ).strip()
            if not sku or not operador:
                log.warning("taecel_producto_sin_identidad", fila_keys=sorted(fila))
                continue

            crudo_monto = fila.get("monto") or fila.get("Monto") or fila.get("precio")
            monto: Money | None = None
            if crudo_monto not in (None, ""):
                try:
                    monto = Money.parse(str(crudo_monto), CURRENCY_MX)
                except (ValueError, ArithmeticError, TypeError):
                    log.warning("taecel_monto_ilegible", sku=sku, crudo=crudo_monto)
                    continue

            productos.append(
                CatalogProduct(
                    provider_slug=self.slug,
                    provider_product_id=sku,
                    operator_code=operador,
                    operator_name=operador,
                    label=str(
                        fila.get("descripcion") or fila.get("Descripcion") or sku
                    )[:160],
                    amount=monto,
                    raw=dict(fila),
                )
            )
        return productos

    # -- recarga -----------------------------------------------------------

    def send_topup(self, request: TopupRequest) -> TopupResult:
        """Envia la recarga. Solo se llama con la orden ya en PAID.

        TAECEL no documenta idempotencia, asi que **aqui no se reintenta
        nunca**. Ante timeout se levanta ``ProviderIndeterminateError`` y la
        conciliacion averigua con ``StatusTXN`` que paso realmente antes de
        que nadie decida reintentar o reembolsar. Reintentar a ciegas una
        recarga que quiza ya se aplico la aplica dos veces y la paga dos
        veces.
        """
        self.ensure_ready()

        # ``Producto`` es el SKU de TAECEL, no nuestro precio. Llega resuelto
        # por el mapping comercial verificado; este adaptador no elige SKU.
        cuerpo = {
            CAMPO_PRODUCTO: request.product_id,
            CAMPO_REFERENCIA: request.phone_national,
        }
        # ``Monto`` solo cuando corresponde. Un paquete de denominacion fija
        # lleva su importe dentro del SKU; mandarlo tambien por separado es
        # como se acaba entregando un monto distinto al cobrado. Quien sabe si
        # corresponde es el mapping verificado, que lo dice en esta bandera.
        if not request.amount_in_sku:
            cuerpo[CAMPO_MONTO] = str(request.amount.amount)

        try:
            datos = self._llamar(self.config.path_request_txn, cuerpo)
        except httpx.ReadTimeout as exc:
            log.error(
                "taecel_read_timeout",
                idempotency_key=request.idempotency_key,
                phone_masked=request.phone_masked,
            )
            raise ProviderIndeterminateError(
                provider=self.slug,
                message=(
                    "TAECEL no respondio a tiempo. La recarga pudo haberse "
                    "aplicado; se consultara su estado antes de reintentar."
                ),
                external_reference=request.idempotency_key,
            ) from exc

        estado = self._leer_estado(datos)
        folio = self._leer_folio(datos)

        # Sin folio no hay forma de consultar despues, y sin poder consultar
        # no se puede afirmar nada. Eso es indeterminado, no exito.
        if estado == TopupStatus.SUCCEEDED and not folio:
            raise ProviderIndeterminateError(
                provider=self.slug,
                message=(
                    "TAECEL respondio algo parecido a exito pero sin folio de "
                    "transaccion. Sin folio no se puede verificar: queda en "
                    "revision manual."
                ),
                external_reference=request.idempotency_key,
            )

        return TopupResult(
            status=estado,
            provider_reference=folio,
            provider_mode=str(self.mode),
            failure_reason=(
                str(datos.get("message") or datos.get("Mensaje") or "")[:255]
                if estado == TopupStatus.FAILED
                else ""
            ),
            delivered_amount=request.amount if estado == TopupStatus.SUCCEEDED else None,
            raw_response=datos,
        )

    def get_topup_status(self, provider_reference: str) -> TopupResult:
        """Consulta el estado real con ``StatusTXN``. Es la conciliacion."""
        self.ensure_ready()

        datos = self._llamar(
            self.config.path_status_txn, {CAMPO_TRANS_ID: provider_reference}
        )
        estado = self._leer_estado(datos)

        return TopupResult(
            status=estado,
            provider_reference=self._leer_folio(datos) or provider_reference,
            provider_mode=str(self.mode),
            failure_reason=str(datos.get("message") or datos.get("Mensaje") or "")[:255],
            raw_response=datos,
        )

    def find_by_custom_identifier(self, custom_identifier: str) -> TopupResult | None:
        """TAECEL no documenta busqueda por referencia propia: devuelve ``None``.

        ``None`` significa "no lo se", que es la respuesta segura: la recarga
        queda en revision manual en vez de arriesgar un duplicado. Si su
        manual resulta tener un endpoint de busqueda, aqui va.
        """
        return None

    # -- internos ----------------------------------------------------------

    def _llamar(self, ruta: str, cuerpo: dict[str, Any]) -> dict[str, Any]:
        """Hace la llamada y devuelve el JSON. Key y NIP se agregan aqui.

        Las credenciales se inyectan en un solo lugar para que ningun metodo
        pueda olvidarlas ni, peor, registrarlas en un log.
        """
        carga = {CAMPO_KEY: self.config.key, CAMPO_NIP: self.config.nip, **cuerpo}

        # Nunca se registran Key ni NIP. Se registra la ruta y las llaves del
        # cuerpo, que es lo que sirve para depurar.
        log.info("taecel_request", ruta=ruta, campos=sorted(cuerpo))

        with self._client() as client:
            if ENVIO_FORM_ENCODED:
                respuesta = client.post(ruta, data=carga)
            else:
                respuesta = client.post(ruta, json=carga)

        if respuesta.status_code in (401, 403):
            raise ProviderPermanentError(
                provider=self.slug,
                message="TAECEL rechazo las credenciales.",
                external_code=str(respuesta.status_code),
            )
        if respuesta.status_code >= 500:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"TAECEL respondio {respuesta.status_code}.",
                external_code=str(respuesta.status_code),
            )
        if respuesta.status_code >= 400:
            raise ProviderPermanentError(
                provider=self.slug,
                message=f"TAECEL respondio {respuesta.status_code} en {ruta}.",
                external_code=str(respuesta.status_code),
            )

        try:
            datos = respuesta.json() if respuesta.content else {}
        except ValueError as exc:
            # Una respuesta que no es JSON no se interpreta a la fuerza.
            raise ProviderTransientError(
                provider=self.slug,
                message="TAECEL devolvio una respuesta que no es JSON.",
            ) from exc

        return datos if isinstance(datos, dict) else {"data": datos}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(
                connect=5.0,
                read=self.config.timeout_seconds,
                write=self.config.timeout_seconds,
                pool=5.0,
            ),
        )

    @staticmethod
    def _buscar(datos: dict[str, Any], llaves: tuple[str, ...]) -> Any:
        """Primera llave presente, mirando tambien un nivel de anidado.

        Existe porque no sabemos si TAECEL responde plano o envuelto en
        ``data``. Buscar en los dos sitios es mas honesto que apostar por uno.
        """
        for llave in llaves:
            if llave in datos:
                return datos[llave]
        anidado = datos.get("data")
        if isinstance(anidado, dict):
            for llave in llaves:
                if llave in anidado:
                    return anidado[llave]
        return None

    def _leer_estado(self, datos: dict[str, Any]) -> str:
        """Traduce el estado de TAECEL al nuestro.

        **Por omision devuelve UNKNOWN, no FAILED y jamas SUCCEEDED.** Los
        nombres de campo y los valores de esta traduccion no estan
        confirmados contra documentacion de TAECEL, asi que lo que no se
        reconoce va a conciliacion. Un UNKNOWN cuesta una consulta; un
        SUCCEEDED inventado cobra un servicio que nadie recibio.
        """
        crudo = self._buscar(datos, LLAVES_ESTADO)
        if crudo is None:
            log.warning("taecel_estado_no_encontrado", llaves=sorted(datos))
            return TopupStatus.UNKNOWN

        if isinstance(crudo, bool):
            return TopupStatus.SUCCEEDED if crudo else TopupStatus.FAILED

        valor = str(crudo).strip().upper()
        if valor in ESTADOS_EXITO:
            return TopupStatus.SUCCEEDED
        if valor in ESTADOS_FALLO:
            return TopupStatus.FAILED
        if valor in ESTADOS_PENDIENTE:
            return TopupStatus.PENDING

        log.warning("taecel_estado_desconocido", valor=valor)
        return TopupStatus.UNKNOWN

    def _leer_folio(self, datos: dict[str, Any]) -> str:
        crudo = self._buscar(datos, LLAVES_FOLIO)
        return str(crudo).strip() if crudo not in (None, "") else ""

    def _leer_saldo(self, datos: dict[str, Any]) -> float | None:
        crudo = self._buscar(datos, LLAVES_SALDO)
        if crudo in (None, ""):
            return None
        try:
            # Se limpian separadores de miles y signo de pesos: TAECEL puede
            # devolver "1,250.00" o "$1,250.00" y float() no los acepta.
            return float(str(crudo).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            log.warning("taecel_saldo_ilegible", crudo=str(crudo)[:40])
            return None
