"""Adaptador de Conekta (pasarela de pago mexicana).

Por que Conekta como primera pasarela (investigacion de septiembre 2026,
detalle en ``docs/api-integrations.md``):

* Es la unica de las evaluadas con **sandbox de auto-servicio documentado**:
  se crea una cuenta, se activa el modo de prueba y se obtienen llaves el
  mismo dia, sin firmar contrato.
* Publica su comision de forma explicita (3.4% + $3.00 MXN por transaccion
  con tarjeta, sin IVA).
* **Firma sus webhooks con un algoritmo publicado** (RSA-SHA256, cabecera
  ``digest``). Mercado Pago firma pero no documenta el algoritmo con la misma
  claridad, y sin eso no se puede confiar en un webhook que mueve dinero.
* Tiene checkout hospedado y tokenizacion, lo que mantiene el numero de
  tarjeta fuera de nuestros servidores y reduce el alcance PCI DSS a SAQ A.

Se descarto el QR de Mercado Pago porque esta **discontinuado desde julio de
2023**, y CoDi porque no esta disponible como API para quien no sea una
institucion financiera regulada ante Banxico.

ESTADO: sin credenciales. El adaptador esta escrito, pero ``check_health()``
devuelve ``NOT_CONFIGURED`` y toda operacion se detiene antes de tocar la
orden. No existe ningun camino que produzca un cobro ficticio.

Nota sobre el SDK: se usa ``httpx`` directamente en vez del SDK oficial para
tener control explicito sobre la cabecera de idempotencia, los timeouts y la
captura de la respuesta cruda para auditoria. En una integracion de pagos esos
tres puntos son mas importantes que el azucar sintactico del SDK.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Final

import httpx
import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from apps.providers.base import (
    PaymentIntent,
    PaymentOutcome,
    PaymentProvider,
    PaymentResult,
    RefundResult,
    WebhookEvent,
)
from apps.providers.registry import payment_registry
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

log = structlog.get_logger("provider.conekta")

API_BASE: Final[str] = "https://api.conekta.io"
#: Version de la API fijada explicitamente. Sin esto, Conekta podria servir una
#: version distinta y cambiar el formato de respuesta sin aviso.
API_VERSION: Final[str] = "2.1.0"

#: Estados de Conekta que significan "el dinero esta confirmado".
_CONFIRMED_STATUSES: Final[frozenset[str]] = frozenset({"paid"})
_PENDING_STATUSES: Final[frozenset[str]] = frozenset({"pending_payment", "partially_paid"})
_DECLINED_STATUSES: Final[frozenset[str]] = frozenset(
    {"declined", "expired", "canceled", "voided"}
)


@dataclass(frozen=True, slots=True)
class ConektaConfig:
    private_key: str
    public_key: str = ""
    #: Llave publica RSA con la que Conekta firma sus webhooks.
    webhook_public_key: str = ""
    timeout_seconds: float = 20.0


@payment_registry.register
class ConektaProvider(PaymentProvider):
    """Cobro con tarjeta a traves de Conekta."""

    slug = "conekta"
    display_name = "Conekta"
    capabilities = frozenset(
        {
            ProviderCapability.CARD_PAYMENT,
            ProviderCapability.BANK_TRANSFER,
            ProviderCapability.HOSTED_CHECKOUT,
            ProviderCapability.TOKENIZATION,
            ProviderCapability.WEBHOOK_SIGNED,
            ProviderCapability.REFUND,
        }
    )
    required_settings = (
        "CONEKTA_PRIVATE_KEY",
        "CONEKTA_PUBLIC_KEY",
        "CONEKTA_WEBHOOK_PUBLIC_KEY",
    )
    requires_commercial_contract = False  # el sandbox no lo requiere
    documentation_url = "https://developers.conekta.com/"

    # -- salud -----------------------------------------------------------

    def check_health(self) -> ProviderHealth:
        missing = []
        if not self.config.private_key:
            missing.append("CONEKTA_PRIVATE_KEY")
        if not self.config.webhook_public_key:
            missing.append("CONEKTA_WEBHOOK_PUBLIC_KEY")

        if missing:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=(
                    "Conekta no tiene credenciales. Crea una cuenta en "
                    "panel.conekta.com, activa el modo de prueba y copia las "
                    "llaves al archivo .env."
                ),
                missing_requirements=tuple(missing),
            )

        # Con credenciales presentes se verifica que REALMENTE funcionen. Dar
        # por bueno un READY solo porque la variable no esta vacia seria
        # exactamente el tipo de suposicion que este proyecto evita.
        try:
            with self._client() as client:
                response = client.get("/orders", params={"limit": 1})
        except httpx.HTTPError as exc:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=f"No se pudo contactar a Conekta: {exc}",
            )

        if response.status_code == 401:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail="Conekta rechazo las credenciales (401). Revisa la llave privada.",
                missing_requirements=("CONEKTA_PRIVATE_KEY valida",),
            )
        if response.status_code >= 500:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=f"Conekta respondio {response.status_code}.",
            )

        return ProviderHealth(
            status=ProviderStatus.READY,
            detail=f"Conekta operativo en modo {self.mode}.",
            latency_ms=int(response.elapsed.total_seconds() * 1000),
        )

    # -- cobro -------------------------------------------------------------

    def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Crea una orden de cobro en Conekta con checkout hospedado.

        Se usa checkout hospedado, no captura de tarjeta propia: asi el numero
        de tarjeta nunca pasa por nuestros servidores y el alcance PCI DSS se
        mantiene en SAQ A. Ver ``docs/security.md``.
        """
        self.ensure_ready()

        payload: dict[str, Any] = {
            "line_items": [
                {
                    "name": intent.description[:120],
                    "unit_price": intent.amount.cents,
                    "quantity": 1,
                }
            ],
            "currency": intent.amount.currency,
            "metadata": {
                "samy_order_id": str(intent.order_id),
                "samy_folio": intent.folio,
                "samy_store_id": str(intent.store_id),
            },
            "checkout": {
                "type": "Integration",
                "allowed_payment_methods": ["card"],
                # Conekta espera minutos de vigencia.
                "expires_at": int(intent.expires_at.timestamp())
                if intent.expires_at
                else None,
            },
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        try:
            with self._client() as client:
                response = client.post(
                    "/orders",
                    json=payload,
                    # Conekta respeta esta cabecera: dos llamadas con la misma
                    # clave producen un solo cargo. Es la defensa contra el
                    # doble cobro por reintento.
                    headers={"X-Idempotency-Key": intent.idempotency_key},
                )
        except httpx.ReadTimeout as exc:
            # El cuerpo ya salio. El cargo pudo haberse creado. NO se asume
            # fallo: la orden entra a conciliacion y se consulta por la clave
            # de idempotencia antes de decidir.
            log.error(
                "conekta_read_timeout",
                order_id=str(intent.order_id),
                idempotency_key=intent.idempotency_key,
            )
            raise ProviderIndeterminateError(
                provider=self.slug,
                message=(
                    "Conekta no respondio a tiempo tras recibir la peticion. "
                    "El cobro pudo haberse creado."
                ),
                external_reference=intent.idempotency_key,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug, message=f"Error de red con Conekta: {exc}"
            ) from exc

        data = self._parse(response)

        if response.status_code >= 500:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Conekta respondio {response.status_code}.",
                external_code=str(response.status_code),
            )
        if response.status_code >= 400:
            raise ProviderPermanentError(
                provider=self.slug,
                message=self._error_message(data),
                external_code=str(response.status_code),
            )

        status = str(data.get("payment_status") or "")
        checkout = data.get("checkout") or {}

        return PaymentResult(
            outcome=self._map_outcome(status),
            provider_reference=str(data.get("id", "")),
            provider_mode=str(self.mode),
            checkout_url=str(checkout.get("url", "")),
            raw_response=self._safe(data),
        )

    def get_payment_status(self, provider_reference: str) -> PaymentResult:
        """Consulta el estado real. Es el mecanismo de conciliacion."""
        self.ensure_ready()

        try:
            with self._client() as client:
                response = client.get(f"/orders/{provider_reference}")
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug, message=f"No se pudo consultar el cobro: {exc}"
            ) from exc

        data = self._parse(response)
        if response.status_code == 404:
            # La orden no existe en Conekta: el cobro nunca se creo. Este es
            # un resultado NEGATIVO confirmado, no un estado desconocido.
            return PaymentResult(
                outcome=PaymentOutcome.DECLINED,
                provider_reference=provider_reference,
                provider_mode=str(self.mode),
                declined_reason="El cobro no existe en Conekta.",
                raw_response={"http_status": 404},
            )
        if response.status_code >= 400:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Conekta respondio {response.status_code} al consultar.",
                external_code=str(response.status_code),
            )

        return PaymentResult(
            outcome=self._map_outcome(str(data.get("payment_status") or "")),
            provider_reference=str(data.get("id", provider_reference)),
            provider_mode=str(self.mode),
            raw_response=self._safe(data),
        )

    def refund(self, provider_reference: str, amount: Money) -> RefundResult:
        self.ensure_ready()
        try:
            with self._client() as client:
                response = client.post(
                    f"/orders/{provider_reference}/refunds",
                    json={"reason": "requested_by_client", "amount": amount.cents},
                )
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug, message=f"Error al reembolsar: {exc}"
            ) from exc

        data = self._parse(response)
        if response.status_code >= 400:
            return RefundResult(
                succeeded=False,
                provider_reference=provider_reference,
                amount=amount,
                failure_reason=self._error_message(data),
                raw_response=self._safe(data),
            )
        return RefundResult(
            succeeded=True,
            provider_reference=str(data.get("id", provider_reference)),
            amount=amount,
            raw_response=self._safe(data),
        )

    # -- webhooks -----------------------------------------------------------

    def verify_webhook(self, *, payload: bytes, headers: dict[str, str]) -> WebhookEvent:
        """Verifica la firma RSA-SHA256 del webhook.

        Conekta firma el cuerpo con su llave privada y envia la firma en la
        cabecera ``digest``, en base64. Se verifica con la llave publica del
        panel.

        Si la firma no valida, se levanta. **Nunca** se procesa un webhook sin
        verificar: un webhook es una instruccion para marcar dinero como
        cobrado, y aceptarlo sin firma equivale a dejar que cualquiera en
        internet marque ordenes como pagadas.
        """
        self.require(ProviderCapability.WEBHOOK_SIGNED)

        if not self.config.webhook_public_key:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    "No hay llave publica de webhook configurada. "
                    "No se puede verificar la firma, asi que el evento se rechaza."
                ),
            )

        signature_b64 = (
            headers.get("digest")
            or headers.get("Digest")
            or headers.get("HTTP_DIGEST")
            or ""
        )
        if not signature_b64:
            raise ProviderPermanentError(
                provider=self.slug,
                message="El webhook llego sin cabecera de firma 'digest'.",
            )

        try:
            public_key = serialization.load_pem_public_key(
                self.config.webhook_public_key.encode()
            )
            public_key.verify(
                base64.b64decode(signature_b64),
                payload,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, ValueError) as exc:
            log.error("conekta_webhook_invalid_signature", error=str(exc))
            raise ProviderPermanentError(
                provider=self.slug,
                message="La firma del webhook no es valida. Evento rechazado.",
            ) from exc

        try:
            body = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderPermanentError(
                provider=self.slug, message="El cuerpo del webhook no es JSON valido."
            ) from exc

        data_object = (body.get("data") or {}).get("object") or {}
        amount_cents = data_object.get("amount")

        return WebhookEvent(
            event_id=str(body.get("id", "")),
            event_type=str(body.get("type", "")),
            provider_reference=str(data_object.get("id", "")),
            outcome=self._map_outcome(str(data_object.get("payment_status") or "")),
            amount=Money(int(amount_cents)) if amount_cents is not None else None,
            raw_payload=self._safe(body),
        )

    # -- internos ------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=API_BASE,
            auth=(self.config.private_key, ""),
            headers={
                "Accept": f"application/vnd.conekta-v{API_VERSION}+json",
                "Content-Type": "application/json",
                "Accept-Language": "es",
                "X-Conekta-Client-User-Agent": json.dumps(
                    {"agent": "samy-cloud", "version": "0.1.0"}
                ),
            },
            timeout=httpx.Timeout(
                connect=5.0,
                read=self.config.timeout_seconds,
                write=self.config.timeout_seconds,
                pool=5.0,
            ),
        )

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {"_raw_text": response.text[:1000]}
        return data if isinstance(data, dict) else {"_raw": data}

    @staticmethod
    def _map_outcome(status: str) -> str:
        if status in _CONFIRMED_STATUSES:
            return PaymentOutcome.CONFIRMED
        if status in _PENDING_STATUSES:
            return PaymentOutcome.PENDING
        if status in _DECLINED_STATUSES:
            return PaymentOutcome.DECLINED
        # Un estado que no conocemos NO se interpreta como exito ni como
        # fallo. Se marca desconocido y la orden va a conciliacion.
        return PaymentOutcome.UNKNOWN

    @staticmethod
    def _error_message(data: dict[str, Any]) -> str:
        details = data.get("details")
        if isinstance(details, list) and details:
            first = details[0]
            if isinstance(first, dict):
                return str(first.get("message") or first.get("debug_message") or data)
        return str(data.get("message") or "Conekta rechazo la operacion.")

    @staticmethod
    def _safe(data: dict[str, Any]) -> dict[str, Any]:
        """Elimina cualquier dato de tarjeta antes de persistir la respuesta.

        Conekta no devuelve el PAN completo, pero guardar respuestas crudas de
        una pasarela sin filtrar es una via clasica de fuga de datos de
        tarjeta hacia la base y los logs.
        """
        forbidden = {"number", "cvc", "cvv", "card_number", "exp_month", "exp_year"}

        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    k: ("[REDACTED]" if k.lower() in forbidden else scrub(v))
                    for k, v in value.items()
                }
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return value

        return scrub(data)
