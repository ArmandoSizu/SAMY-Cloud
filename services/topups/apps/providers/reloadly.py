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

    def __init__(self, config: ReloadlyConfig, mode: ProviderMode) -> None:
        super().__init__(config, mode)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

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
        """Traduce un operador de Reloadly a productos de nuestro catalogo."""
        operator_id = str(operator.get("operatorId", ""))
        name = str(operator.get("name", "")).strip()
        currency = str(operator.get("destinationCurrencyCode") or "MXN")
        logo_urls = operator.get("logoUrls") or []
        supports_data = bool(operator.get("bundle"))

        products: list[CatalogProduct] = []

        # Denominaciones fijas: la lista exacta de montos que el operador vende.
        for raw_amount in operator.get("fixedAmounts") or []:
            try:
                amount = Money.parse(str(raw_amount), currency)
            except (ValueError, ArithmeticError):
                continue

            descriptions = operator.get("fixedAmountsDescriptions") or {}
            label = str(descriptions.get(str(raw_amount)) or f"Recarga {amount}")

            products.append(
                CatalogProduct(
                    provider_slug=self.slug,
                    provider_product_id=f"{operator_id}:{raw_amount}",
                    operator_code=operator_id,
                    operator_name=name,
                    label=label,
                    amount=amount,
                    is_data_package=supports_data,
                    logo_url=str(logo_urls[0]) if logo_urls else "",
                    raw={"operator": operator_id, "amount": raw_amount},
                )
            )

        # Rango libre: algunos operadores aceptan cualquier monto entre un
        # minimo y un maximo. Se expone como producto de monto abierto.
        min_amount = operator.get("minAmount")
        max_amount = operator.get("maxAmount")
        if min_amount and max_amount and not products:
            products.append(
                CatalogProduct(
                    provider_slug=self.slug,
                    provider_product_id=f"{operator_id}:range",
                    operator_code=operator_id,
                    operator_name=name,
                    label=f"Monto libre ({min_amount} - {max_amount})",
                    amount=None,
                    min_amount=Money.parse(str(min_amount), currency),
                    max_amount=Money.parse(str(max_amount), currency),
                    is_data_package=supports_data,
                    logo_url=str(logo_urls[0]) if logo_urls else "",
                    raw={"operator": operator_id, "range": [min_amount, max_amount]},
                )
            )

        return products

    # -- recarga -----------------------------------------------------------

    def send_topup(self, request: TopupRequest) -> TopupResult:
        """Envia la recarga. Solo se llama con la orden ya en PAID."""
        self.ensure_ready()

        payload = {
            "operatorId": int(request.operator_code),
            "amount": float(request.amount.amount),
            "useLocalAmount": True,
            "customIdentifier": request.idempotency_key,
            "recipientPhone": {
                "countryCode": COUNTRY_MX,
                "number": request.phone_e164,
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
        """Consulta el estado real. Mecanismo de conciliacion."""
        self.ensure_ready()
        try:
            with self._client() as client:
                response = client.get(f"/topups/reports/transactions/{provider_reference}")
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug, message=f"No se pudo consultar la recarga: {exc}"
            ) from exc

        if response.status_code == 404:
            return TopupResult(
                status=TopupStatus.FAILED,
                provider_reference=provider_reference,
                provider_mode=str(self.mode),
                failure_reason="La recarga no existe en Reloadly.",
            )

        data = response.json() if response.content else {}
        return TopupResult(
            status=self._map_status(str(data.get("status") or "").upper()),
            provider_reference=str(data.get("transactionId", provider_reference)),
            provider_mode=str(self.mode),
            operator_reference=str(data.get("operatorTransactionId") or ""),
            raw_response=data,
        )

    # -- internos -----------------------------------------------------------

    def _ensure_token(self) -> str:
        """Obtiene y cachea el token OAuth2.

        Se renueva 60 segundos antes de expirar: pedirlo justo al vencer
        produce fallos intermitentes por desfase de reloj.
        """
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

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
        self._token = str(data["access_token"])
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self._ensure_token()}",
                "Accept": "application/com.reloadly.topups-v1+json",
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
        if status in {"SUCCESSFUL", "SUCCESS", "COMPLETED"}:
            return TopupStatus.SUCCEEDED
        if status in {"PROCESSING", "PENDING"}:
            return TopupStatus.PENDING
        if status in {"FAILED", "REFUNDED", "DECLINED"}:
            return TopupStatus.FAILED
        # Estado desconocido: nunca se interpreta como exito.
        return TopupStatus.UNKNOWN
