"""Adaptador de Reloadly para recargas de tiempo aire.

Por que Reloadly como primer proveedor (investigacion de septiembre 2026,
detalle en ``docs/api-integrations.md``):

* **Telcel, AT&T, Movistar y Unefon NO ofrecen API publica.** La pagina de
  distribuidores de Telcel solo tiene un formulario de contacto; no existe
  documentacion tecnica publica. Toda recarga se vende necesariamente a traves
  de un distribuidor o agregador autorizado.

* De los agregadores evaluados, Reloadly es el **unico con sandbox de
  auto-servicio**: se crea una cuenta, se generan credenciales y se recibe
  saldo de prueba el mismo dia, sin contrato ni levantamiento tecnico.
  Taecel y Sivetel exigen tramite previo y solo entregan documentacion tras
  aprobarlo.

* Reloadly publica cobertura de operadores mexicanos y su catalogo es
  consultable por API, que es justo lo que este microservicio necesita: el
  requisito prohibe codificar denominaciones a mano.

**Reloadly es para desarrollar y demostrar. Para operar comercialmente en
Mexico conviene un distribuidor local (Taecel o similar)** por precio y
cobertura, y el tramite debe iniciarse en paralelo porque toma semanas. Esa es
precisamente la razon de que exista ``TopupProvider``: cambiar de proveedor
sera escribir un adaptador, no reescribir el microservicio.

ESTADO: sin credenciales. ``check_health()`` devuelve NOT_CONFIGURED y toda
operacion se detiene antes de tocar la orden.

DOS COSAS QUE HAY QUE TENER PRESENTES AL LEER ESTE ARCHIVO
----------------------------------------------------------

**1. Dos monedas.** Reloadly maneja la moneda de NUESTRO monedero (que en una
cuenta de sandbox es USD) y la moneda del destinatario (MXN para Mexico), y
devuelve las denominaciones por duplicado: ``fixedAmounts`` viene en la
primera y ``localFixedAmounts`` en la segunda. Aqui se usan SIEMPRE las
locales, y las recargas se envian con ``useLocalAmount=true``. Vender una
recarga de "100" tomada de la lista equivocada significaria cobrar 100 pesos
por lo que en realidad son 100 dolares de saldo.

**2. ``customIdentifier`` NO es una clave de idempotencia.** Reloadly la
documenta como "tu referencia interna" y en ninguna parte promete que dos
peticiones con el mismo valor produzcan una sola recarga. Por eso este
adaptador **nunca reintenta un POST /topups**: ante un timeout levanta
``ProviderIndeterminateError`` y la conciliacion busca la transaccion por esa
referencia con ``find_by_custom_identifier()`` antes de decidir nada.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
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
    ProviderPermanentError,
    ProviderTransientError,
)

log = structlog.get_logger("provider.reloadly")

AUTH_URL: Final[str] = "https://auth.reloadly.com/oauth/token"
SANDBOX_BASE: Final[str] = "https://topups-sandbox.reloadly.com"
PRODUCTION_BASE: Final[str] = "https://topups.reloadly.com"

#: Codigo ISO del pais que atendemos.
COUNTRY_MX: Final[str] = "MX"

#: Moneda en la que vendemos. Es la del destinatario, no la del monedero.
CURRENCY_MX: Final[str] = "MXN"

#: Cabecera que fija la version de la API. Sin ella Reloadly puede servir otra
#: version y cambiar el formato de respuesta sin aviso.
ACCEPT_HEADER: Final[str] = "application/com.reloadly.topups-v1+json"

#: Prefijo de la clave donde se guarda el token OAuth compartido.
#:
#: El registro construye un adaptador NUEVO en cada llamada, asi que un cache
#: en la instancia se pierde siempre y acabariamos pidiendo un token por cada
#: peticion. Reloadly castiga el exceso de llamadas suspendiendo la cuenta, y
#: reactivarla exige hablar con soporte: el cache compartido no es una
#: optimizacion, es proteccion.
TOKEN_CACHE_PREFIX: Final[str] = "samy:reloadly:token:"

#: Margen con el que se renueva el token antes de vencer. Pedirlo justo al
#: expirar produce fallos intermitentes por desfase de reloj entre maquinas.
TOKEN_REFRESH_MARGIN_SECONDS: Final[int] = 300

#: Reloadly responde 404 con este codigo cuando no reconoce al operador.
ERROR_NO_AUTODETECT: Final[str] = "COULD_NOT_AUTO_DETECT_OPERATOR"


@dataclass(frozen=True, slots=True)
class ReloadlyConfig:
    client_id: str
    client_secret: str
    timeout_seconds: float = 25.0


@topup_registry.register
class ReloadlyProvider(TopupProvider):
    """Recargas de tiempo aire via Reloadly."""

    slug = "reloadly"
    display_name = "Reloadly"
    capabilities = frozenset(
        {
            ProviderCapability.AIRTIME_TOPUP,
            ProviderCapability.DATA_PACKAGE,
            ProviderCapability.CATALOG_SYNC,
        }
    )
    required_settings = ("RELOADLY_CLIENT_ID", "RELOADLY_CLIENT_SECRET")
    requires_commercial_contract = False
    documentation_url = "https://docs.reloadly.com/airtime"

    @property
    def base_url(self) -> str:
        return (
            SANDBOX_BASE if self.mode == ProviderMode.SANDBOX else PRODUCTION_BASE
        )

    # -- salud ------------------------------------------------------------

    def check_health(self) -> ProviderHealth:
        missing = [
            name
            for name, value in (
                ("RELOADLY_CLIENT_ID", self.config.client_id),
                ("RELOADLY_CLIENT_SECRET", self.config.client_secret),
            )
            if not value
        ]
        if missing:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=(
                    "Reloadly no tiene credenciales. Crea una cuenta gratuita en "
                    "reloadly.com, entra a Developers > API Settings, genera una "
                    "aplicacion y copia Client ID y Client Secret al archivo .env."
                ),
                missing_requirements=tuple(missing),
            )

        started = time.perf_counter()
        try:
            self._ensure_token()
            with self._client() as client:
                response = client.get("/accounts/balance")
        except ProviderPermanentError as exc:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=f"Reloadly rechazo las credenciales: {exc.message}",
                missing_requirements=("Credenciales validas de Reloadly",),
            )
        except (httpx.HTTPError, ProviderTransientError) as exc:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=f"No se pudo contactar a Reloadly: {exc}",
            )

        if response.status_code >= 400:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=f"Reloadly respondio {response.status_code} al consultar saldo.",
            )

        balance = response.json() if response.content else {}
        amount = balance.get("balance")

        # Saldo agotado significa que NO se puede recargar. Reportarlo como
        # READY produciria un fallo en medio de una venta ya cobrada.
        if isinstance(amount, (int, float)) and amount <= 0:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    f"Reloadly esta configurado pero el saldo es {amount} "
                    f"{balance.get('currencyCode', '')}. Hay que fondear la cuenta."
                ),
                missing_requirements=("Saldo disponible en Reloadly",),
            )

        return ProviderHealth(
            status=ProviderStatus.READY,
            detail=(
                f"Reloadly operativo en modo {self.mode}. "
                f"Saldo: {amount} {balance.get('currencyCode', '')}."
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # -- catalogo ----------------------------------------------------------

    def fetch_catalog(self) -> list[CatalogProduct]:
        """Descarga el catalogo REAL de operadores y denominaciones de Mexico.

        El requisito es explicito: no se inventan denominaciones. Si el
        proveedor no ofrece $75, SAMY Cloud no muestra $75. Lo que devuelve
        este metodo es literalmente lo que se puede vender.
        """
        self.ensure_ready()

        products: list[CatalogProduct] = []
        page = 0

        while True:
            with self._client() as client:
                response = client.get(
                    "/operators/countries/" + COUNTRY_MX,
                    params={"page": page, "size": 100, "includeBundles": "true"},
                )

            if response.status_code >= 400:
                raise ProviderTransientError(
                    provider=self.slug,
                    message=f"Reloadly respondio {response.status_code} al pedir el catalogo.",
                    external_code=str(response.status_code),
                )

            payload = response.json()
            # Reloadly devuelve una lista simple o un objeto paginado segun el
            # endpoint; se soportan ambas formas para no romper si cambia.
            operators = payload if isinstance(payload, list) else payload.get("content", [])
            if not operators:
                break

            for operator in operators:
                products.extend(self._parse_operator(operator))

            if isinstance(payload, list) or payload.get("last", True):
                break
            page += 1

        log.info("reloadly_catalog_fetched", products=len(products))
        return products

    def _parse_operator(self, operator: dict[str, Any]) -> list[CatalogProduct]:
        """Traduce un operador de Reloadly a productos de nuestro catalogo.

        **Se usan las denominaciones LOCALES.** Reloadly devuelve cada monto
        dos veces: ``fixedAmounts`` en la moneda de nuestro monedero (USD en
        una cuenta de sandbox) y ``localFixedAmounts`` en la del destinatario
        (MXN). El cajero cobra pesos y el cliente recibe pesos, asi que la
        unica lista correcta es la local. Tomar la otra significaria vender
        "100" cobrando 100 pesos por 100 dolares de saldo.
        """
        operator_id = str(operator.get("operatorId") or operator.get("id") or "")
        name = str(operator.get("name", "")).strip()
        currency = str(operator.get("destinationCurrencyCode") or CURRENCY_MX)
        logo_urls = operator.get("logoUrls") or []
        # ``data`` marca paquetes de datos; ``bundle`` marca combos. Cualquiera
        # de los dos deja de ser "tiempo aire" a secas.
        is_data = bool(operator.get("data")) or bool(operator.get("bundle"))

        products: list[CatalogProduct] = []

        amounts = operator.get("localFixedAmounts") or []
        descriptions = operator.get("localFixedAmountsDescriptions") or {}

        for raw_amount in amounts:
            try:
                amount = Money.parse(str(raw_amount), currency)
            except (ValueError, ArithmeticError, TypeError):
                # Un monto que no se puede interpretar NO se vende. Preferimos
                # un catalogo con un hueco a una denominacion mal calculada.
                log.warning(
                    "reloadly_amount_unparseable",
                    operator=operator_id,
                    raw_amount=raw_amount,
                )
                continue

            # Reloadly indexa las descripciones por el monto formateado con
            # dos decimales ("50.00"), no por el valor crudo.
            label = str(
                descriptions.get(f"{float(raw_amount):.2f}")
                or descriptions.get(str(raw_amount))
                or f"Recarga {amount}"
            )

            products.append(
                CatalogProduct(
                    provider_slug=self.slug,
                    provider_product_id=f"{operator_id}:{raw_amount}",
                    operator_code=operator_id,
                    operator_name=name,
                    label=label[:160],
                    amount=amount,
                    is_data_package=is_data,
                    logo_url=str(logo_urls[0]) if logo_urls else "",
                    raw={
                        "operator": operator_id,
                        "local_amount": raw_amount,
                        "currency": currency,
                        "denomination_type": operator.get("denominationType"),
                    },
                )
            )

        # Monto libre. La regla que publica Reloadly es mirar el minimo: si es
        # nulo, el operador es de denominaciones fijas; si trae valor, acepta
        # cualquier monto del rango.
        min_amount = operator.get("localMinAmount")
        max_amount = operator.get("localMaxAmount")
        es_rango = (
            str(operator.get("denominationType") or "").upper() == "RANGE"
            or min_amount is not None
        )

        if es_rango and min_amount is not None and max_amount is not None:
            try:
                minimo = Money.parse(str(min_amount), currency)
                maximo = Money.parse(str(max_amount), currency)
            except (ValueError, ArithmeticError, TypeError):
                return products

            products.append(
                CatalogProduct(
                    provider_slug=self.slug,
                    provider_product_id=f"{operator_id}:range",
                    operator_code=operator_id,
                    operator_name=name,
                    label=f"Monto libre ({minimo} a {maximo})",
                    amount=None,
                    min_amount=minimo,
                    max_amount=maximo,
                    is_data_package=is_data,
                    logo_url=str(logo_urls[0]) if logo_urls else "",
                    raw={
                        "operator": operator_id,
                        "local_range": [min_amount, max_amount],
                        "currency": currency,
                        "denomination_type": operator.get("denominationType"),
                    },
                )
            )

        return products

    # -- deteccion de operador ---------------------------------------------

    def detect_operator(self, phone_national: str) -> dict[str, Any] | None:
        """Averigua a que compania pertenece un numero. ``None`` si no lo sabe.

        Es una AYUDA, no una garantia. Con la portabilidad numerica vigente en
        Mexico desde 2008, el prefijo no dice nada, asi que preguntarle al
        proveedor es lo unico honesto que se puede hacer. Aun asi, la eleccion
        final la confirma el cajero: si Reloadly se equivoca, la recarga se va
        a la compania incorrecta y el dinero no vuelve.

        Un numero recien portado es justamente el caso que Reloadly puede
        fallar, y por eso devolver ``None`` es un resultado normal, no un
        error que deba interrumpir la venta.
        """
        self.ensure_ready()

        try:
            with self._client() as client:
                response = client.get(
                    f"/operators/auto-detect/phone/{phone_national}/countries/{COUNTRY_MX}"
                )
        except httpx.HTTPError as exc:
            # Que falle la deteccion no puede impedir vender: el cajero elige
            # la compania a mano, como ha hecho siempre.
            log.warning("reloadly_autodetect_unavailable", error=str(exc))
            return None

        if response.status_code == 404:
            log.info("reloadly_operator_not_detected", phone_masked=phone_national[-4:])
            return None
        if response.status_code >= 400:
            log.warning(
                "reloadly_autodetect_failed", status=response.status_code
            )
            return None

        data = response.json() if response.content else {}
        if not isinstance(data, dict) or not data.get("operatorId"):
            return None

        return {
            "provider_operator_id": str(data.get("operatorId")),
            "name": str(data.get("name", "")),
        }

    # -- recarga -----------------------------------------------------------

    def send_topup(self, request: TopupRequest) -> TopupResult:
        """Envia la recarga. Solo se llama con la orden ya en PAID."""
        self.ensure_ready()

        # ``useLocalAmount=True`` es obligatorio y va de la mano del catalogo:
        # los montos que vendemos salen de ``localFixedAmounts``, es decir en
        # pesos. Enviarlos sin esta bandera los interpretaria como dolares.
        #
        # El numero viaja en formato nacional de 10 digitos junto al codigo de
        # pais. Reloadly normaliza ambas formas, pero sus propios ejemplos se
        # contradicen sobre si el numero debe llevar el 52; mandar el nacional
        # con el pais aparte no es ambiguo en ninguna de las dos lecturas.
        payload = {
            "operatorId": int(request.operator_code),
            "amount": float(request.amount.amount),
            "useLocalAmount": True,
            # Referencia NUESTRA para conciliar. Reloadly no promete que
            # deduplique por este campo, asi que no se usa como salvaguarda.
            "customIdentifier": request.idempotency_key,
            "recipientPhone": {
                "countryCode": COUNTRY_MX,
                "number": request.phone_national,
            },
        }

        try:
            with self._client() as client:
                response = client.post("/topups", json=payload)
        except httpx.ReadTimeout as exc:
            # La recarga pudo haberse enviado. NO se asume fallo: se consulta
            # despues por customIdentifier antes de decidir.
            log.error(
                "reloadly_read_timeout",
                idempotency_key=request.idempotency_key,
                phone_masked=request.phone_masked,
            )
            raise ProviderIndeterminateError(
                provider=self.slug,
                message=(
                    "Reloadly no respondio a tiempo. La recarga pudo haberse "
                    "aplicado; se consultara antes de reintentar."
                ),
                external_reference=request.idempotency_key,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug, message=f"Error de red con Reloadly: {exc}"
            ) from exc

        data = response.json() if response.content else {}

        if response.status_code >= 500:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Reloadly respondio {response.status_code}.",
                external_code=str(response.status_code),
            )
        if response.status_code >= 400:
            raise ProviderPermanentError(
                provider=self.slug,
                message=str(data.get("message") or "Reloadly rechazo la recarga."),
                external_code=str(data.get("errorCode") or response.status_code),
            )

        status_raw = str((data.get("status") or "")).upper()
        return TopupResult(
            status=self._map_status(status_raw),
            provider_reference=str(data.get("transactionId", "")),
            provider_mode=str(self.mode),
            operator_reference=str(data.get("operatorTransactionId") or ""),
            delivered_amount=Money.parse(
                str(data.get("deliveredAmount") or request.amount.amount),
                request.amount.currency,
            ),
            raw_response=data,
        )

    def get_topup_status(self, provider_reference: str) -> TopupResult:
        """Consulta el estado real. Mecanismo de conciliacion.

        La respuesta viene envuelta: ``{code, message, status, transaction}``.
        Mientras la recarga esta en curso, ``transaction`` es ``null`` y solo
        hay ``status``; al terminar llega la transaccion completa. Por eso se
        leen los datos del sobre y del contenido con cuidado, en vez de
        suponer que la transaccion siempre viene.
        """
        self.ensure_ready()
        try:
            with self._client() as client:
                response = client.get(f"/topups/{provider_reference}/status")
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug, message=f"No se pudo consultar la recarga: {exc}"
            ) from exc

        if response.status_code == 404:
            # Reloadly no conoce esa transaccion. Es un resultado NEGATIVO
            # confirmado: la recarga no llego a existir.
            return TopupResult(
                status=TopupStatus.FAILED,
                provider_reference=provider_reference,
                provider_mode=str(self.mode),
                failure_reason="La recarga no existe en Reloadly.",
            )
        if response.status_code >= 400:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Reloadly respondio {response.status_code} al consultar la recarga.",
                external_code=str(response.status_code),
            )

        sobre = response.json() if response.content else {}
        if not isinstance(sobre, dict):
            sobre = {}
        transaccion = sobre.get("transaction") or {}
        if not isinstance(transaccion, dict):
            transaccion = {}

        return TopupResult(
            status=self._map_status(str(sobre.get("status") or "").upper()),
            provider_reference=str(
                transaccion.get("transactionId") or provider_reference
            ),
            provider_mode=str(self.mode),
            operator_reference=str(transaccion.get("operatorTransactionId") or ""),
            failure_reason=str(sobre.get("message") or "")[:255],
            raw_response=sobre,
        )

    def find_by_custom_identifier(self, custom_identifier: str) -> TopupResult | None:
        """Busca una recarga por NUESTRA referencia. Devuelve ``None`` si no existe.

        Existe por una razon concreta: ``customIdentifier`` no es una clave de
        idempotencia. Cuando ``send_topup`` termina en timeout no sabemos si
        la recarga se aplico, y reintentar a ciegas puede recargar dos veces.
        Este metodo es el que permite averiguarlo antes de decidir.

        Reloadly no publica un endpoint de busqueda por esa referencia, asi
        que se recorre el listado de transacciones recientes y se filtra aqui.
        Es mas caro que una consulta directa; es lo que hay, y sigue siendo
        mucho mas barato que una recarga duplicada.
        """
        self.ensure_ready()

        try:
            with self._client() as client:
                response = client.get(
                    "/reports/transactions", params={"page": 0, "size": 200}
                )
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"No se pudo consultar el historial de Reloadly: {exc}",
            ) from exc

        if response.status_code >= 400:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Reloadly respondio {response.status_code} al listar transacciones.",
                external_code=str(response.status_code),
            )

        payload = response.json() if response.content else {}
        transacciones = (
            payload if isinstance(payload, list) else payload.get("content", []) or []
        )

        for transaccion in transacciones:
            if not isinstance(transaccion, dict):
                continue
            if str(transaccion.get("customIdentifier") or "") != custom_identifier:
                continue

            log.info(
                "reloadly_transaction_found_by_identifier",
                custom_identifier=custom_identifier,
                transaction_id=transaccion.get("transactionId"),
            )
            # Aparecer en el historial significa que Reloadly la acepto. El
            # estado definitivo se pide aparte, porque el listado no siempre
            # lo trae.
            return self.get_topup_status(str(transaccion.get("transactionId")))

        log.info("reloadly_transaction_not_found", custom_identifier=custom_identifier)
        return None

    # -- internos -----------------------------------------------------------

    @property
    def _token_cache_key(self) -> str:
        """Clave por modo y por credencial.

        Incluye el modo para que un token de sandbox no se use jamas contra
        produccion, y un fragmento del client_id para que rotar credenciales
        invalide el cache solo. Nunca se guarda el secreto.
        """
        return f"{TOKEN_CACHE_PREFIX}{self.mode}:{self.config.client_id[:12]}"

    def _ensure_token(self) -> str:
        """Obtiene el token OAuth2, compartido entre procesos via Redis.

        El registro construye un adaptador nuevo en cada llamada, asi que un
        cache en memoria de la instancia no sobrevive a la peticion y
        acabariamos pidiendo un token cada vez. Reloadly responde al exceso de
        llamadas suspendiendo la cuenta, y reactivarla exige escribir a
        soporte: compartir el token no es una optimizacion, es evitar quedarse
        sin proveedor a media jornada.

        El token dura 24 h en sandbox y 60 dias en produccion; se guarda con
        margen para no usar uno a punto de vencer.
        """
        from django.core.cache import cache

        cached = cache.get(self._token_cache_key)
        if cached:
            return str(cached)

        audience = self.base_url
        try:
            response = httpx.post(
                AUTH_URL,
                json={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "client_credentials",
                    "audience": audience,
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug, message=f"No se pudo autenticar con Reloadly: {exc}"
            ) from exc

        if response.status_code in (400, 401, 403):
            raise ProviderPermanentError(
                provider=self.slug,
                message="Reloadly rechazo las credenciales.",
                external_code=str(response.status_code),
            )
        if response.status_code >= 400:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Reloadly devolvio {response.status_code} al autenticar.",
            )

        data = response.json()
        token = str(data["access_token"])
        expires_in = int(data.get("expires_in", 3600))

        # Se guarda con margen: un token que vence mientras viaja la peticion
        # produce un 401 que parece un problema de credenciales y no lo es.
        ttl = max(60, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
        cache.set(self._token_cache_key, token, timeout=ttl)

        log.info("reloadly_token_obtained", mode=str(self.mode), ttl_seconds=ttl)
        return token

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self._ensure_token()}",
                "Accept": ACCEPT_HEADER,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=5.0,
                read=self.config.timeout_seconds,
                write=self.config.timeout_seconds,
                pool=5.0,
            ),
        )

    @staticmethod
    def _map_status(status: str) -> str:
        """Traduce el estado de Reloadly al nuestro.

        Reloadly documenta cuatro: PROCESSING, SUCCESSFUL, REFUNDED y FAILED.
        Se aceptan ademas algunos sinonimos por si aparecen.

        REFUNDED cuenta como fallo NUESTRO aunque para Reloadly sea un final
        feliz: su reembolso automatico devuelve el dinero a nuestro monedero,
        no al bolsillo del cliente, que pago en el mostrador y no recibio su
        recarga. Ese caso dispara REFUND_PENDING en la orden.
        """
        if status in {"SUCCESSFUL", "SUCCESS", "COMPLETED"}:
            return TopupStatus.SUCCEEDED
        if status in {"PROCESSING", "PENDING", "IN_PROGRESS"}:
            return TopupStatus.PENDING
        if status in {"FAILED", "REFUNDED", "DECLINED", "ERROR"}:
            return TopupStatus.FAILED
        # Estado desconocido: nunca se interpreta como exito. La documentacion
        # de Reloadly no garantiza que la lista sea exhaustiva, asi que lo que
        # no reconocemos va a conciliacion en vez de darse por bueno.
        return TopupStatus.UNKNOWN
