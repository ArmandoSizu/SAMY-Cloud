"""Cliente HTTP firmado para comunicacion entre microservicios.

Caracteristicas que un cliente S2S en un sistema de dinero necesita:

* **Firma HMAC** en cada peticion (ver ``samy_common.security.signing``).
* **Propagacion de correlacion**: el ``correlation_id`` viaja para poder
  reconstruir la traza completa de una operacion.
* **Idempotencia**: toda peticion mutante lleva ``Idempotency-Key``, de modo
  que un reintento tras un timeout no duplique la operacion.
* **Reintentos con backoff solo donde es seguro**: se reintentan errores de
  red y 5xx en peticiones idempotentes. Un ``POST`` sin clave de idempotencia
  NUNCA se reintenta automaticamente.
* **Timeouts explicitos**: sin timeout, un proveedor lento bloquea workers y
  tumba el servicio.
* **Distincion transitorio/permanente/indeterminado**: un timeout despues de
  enviar la peticion NO es un fallo, es un estado desconocido y se propaga
  como ``ProviderIndeterminateError`` para que la orden entre a conciliacion.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Final, Mapping

import httpx

from samy_common.observability.logging import correlation_id_var, get_logger
from samy_common.providers.exceptions import (
    ProviderIndeterminateError,
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.security.signing import sign_request

__all__ = ["ServiceClient", "ServiceResponse", "ServiceClientConfig"]

log = get_logger("s2s")

#: Metodos que pueden reintentarse sin riesgo de duplicar efectos.
_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

HEADER_IDEMPOTENCY: Final[str] = "Idempotency-Key"
HEADER_CORRELATION: Final[str] = "X-Correlation-ID"


@dataclass(frozen=True, slots=True)
class ServiceClientConfig:
    base_url: str
    secret: str
    #: Nombre del servicio que llama (va firmado, identifica al emisor).
    caller: str
    connect_timeout: float = 3.0
    read_timeout: float = 15.0
    max_retries: int = 2


@dataclass(frozen=True, slots=True)
class ServiceResponse:
    status_code: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class ServiceClient:
    """Cliente HTTP firmado hacia otro microservicio de SAMY Cloud."""

    def __init__(self, config: ServiceClientConfig) -> None:
        self.config = config
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=config.connect_timeout,
                read=config.read_timeout,
                write=config.read_timeout,
                pool=config.connect_timeout,
            ),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ServiceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- API publica ---------------------------------------------------

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> ServiceResponse:
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ServiceResponse:
        return self._request(
            "POST", path, payload=payload, idempotency_key=idempotency_key
        )

    # -- interno --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ServiceResponse:
        if not path.startswith("/"):
            path = f"/{path}"

        body = b""
        headers: dict[str, str] = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            headers["Content-Type"] = "application/json"

        # Una peticion mutante siempre lleva clave de idempotencia. Si quien
        # llama no la aporta, generamos una estable para este intento logico.
        if method not in _SAFE_METHODS:
            headers[HEADER_IDEMPOTENCY] = idempotency_key or uuid.uuid4().hex

        if cid := correlation_id_var.get():
            headers[HEADER_CORRELATION] = cid

        retryable = method in _SAFE_METHODS or HEADER_IDEMPOTENCY in headers
        attempts = self.config.max_retries + 1 if retryable else 1
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            # La firma se recalcula en cada intento: incluye timestamp y nonce,
            # que deben ser frescos o el receptor los rechaza por replay.
            signed = sign_request(
                secret=self.config.secret,
                method=method,
                path=path,
                service=self.config.caller,
                body=body,
            )
            request_headers = {**headers, **signed.as_dict()}

            try:
                response = self._client.request(
                    method,
                    path,
                    params=dict(params) if params else None,
                    content=body or None,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                # Un timeout de lectura tras enviar el cuerpo deja el resultado
                # indeterminado: el receptor pudo haber procesado la operacion.
                if method not in _SAFE_METHODS and isinstance(
                    exc, httpx.ReadTimeout
                ):
                    log.warning(
                        "s2s_indeterminate",
                        method=method,
                        path=path,
                        attempt=attempt,
                        error=str(exc),
                    )
                    if attempt >= attempts:
                        raise ProviderIndeterminateError(
                            provider=self.config.caller,
                            message=(
                                f"Timeout tras enviar {method} {path}. "
                                "El resultado es desconocido y requiere conciliacion."
                            ),
                        ) from exc
                if attempt >= attempts:
                    raise ProviderTransientError(
                        provider=self.config.caller,
                        message=f"Timeout en {method} {path}: {exc}",
                    ) from exc
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= attempts:
                    raise ProviderTransientError(
                        provider=self.config.caller,
                        message=f"Error de red en {method} {path}: {exc}",
                    ) from exc
                continue

            if response.status_code >= 500:
                log.warning(
                    "s2s_server_error",
                    method=method,
                    path=path,
                    status=response.status_code,
                    attempt=attempt,
                )
                if attempt >= attempts:
                    raise ProviderTransientError(
                        provider=self.config.caller,
                        message=f"{method} {path} devolvio {response.status_code}.",
                        external_code=str(response.status_code),
                    )
                continue

            data = self._decode(response)

            if response.status_code >= 400:
                raise ProviderPermanentError(
                    provider=self.config.caller,
                    message=f"{method} {path} rechazado ({response.status_code}).",
                    external_code=str(response.status_code),
                )

            return ServiceResponse(status_code=response.status_code, data=data)

        raise ProviderTransientError(  # pragma: no cover - inalcanzable
            provider=self.config.caller,
            message=f"Agotados los reintentos de {method} {path}: {last_exc}",
        )

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text
