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
tener control explicito sobre los timeouts, el manejo de respuestas
indeterminadas y la captura de la respuesta cruda para auditoria. En una
integracion de pagos esos tres puntos son mas importantes que el azucar
sintactico del SDK.

LA RESPUESTA SINCRONA NO ES LA VERDAD FINAL
--------------------------------------------

Un ``POST /orders`` con un token de tarjeta suele devolver ya
``payment_status: paid``, y es tentador tratarlo como el desenlace. No lo es.
La documentacion de Conekta es explicita en que el estado de un pago puede
cambiar de forma asincrona: 3D Secure, revisiones antifraude, capturas
diferidas, contracargos y reembolsos ocurren DESPUES de esa respuesta.

Por eso la verdad se construye con tres fuentes, en este orden:

1. **Respuesta inmediata de la API.** Sirve para no dejar al cajero
   esperando, y es suficiente para decidir si se entrega el servicio en ese
   momento. No cierra el caso.
2. **Consulta y conciliacion.** ``reconcile_indeterminate_orders`` pregunta a
   Conekta por las ordenes cuyo desenlace quedo en duda (timeout, respuesta
   ambigua). Es la red de seguridad cuando el webhook no llega.
3. **Webhook firmado.** Es la unica fuente que entera al sistema de lo que
   pasa DESPUES: un pago que se cae, un contracargo, un reembolso. En
   produccion es obligatorio, y sin una URL publica no existe: en local
   Conekta no puede alcanzar http://localhost, asi que el ciclo asincrono
   sencillamente no se ejercita. Eso hay que tenerlo presente al probar.

Ninguna de las tres sobra. La primera da respuesta, la segunda repara y la
tercera entera de los cambios posteriores.

CONEKTA NO TIENE CABECERA DE IDEMPOTENCIA
------------------------------------------

Esto merece un aviso porque es contraintuitivo y porque este archivo llego a
afirmar lo contrario. Se reviso la documentacion oficial (Autenticacion,
Create order, Reintentos de pago) y **Conekta no documenta ninguna cabecera de
idempotencia**; los unicos encabezados que acepta ``POST /orders`` son
``Accept-Language`` y ``X-Child-Company-Id``. Enviar ``X-Idempotency-Key`` no
hace nada: se ignora en silencio, que es la peor forma de fallar porque
parece que protege.

La proteccion contra doble cobro es NUESTRA y son tres capas:

1. Un ``PaymentAttempt`` que ya tiene ``provider_reference`` no vuelve a crear
   una orden en Conekta (``create_payment`` lo rechaza).
2. El token de tarjeta es de un solo uso y caduca en 10 minutos: reutilizarlo
   falla del lado de Conekta.
3. ``confirm_payment`` es idempotente: una orden ya PAID ignora
   confirmaciones repetidas.

Y ``metadata.samy_attempt_id`` permite encontrar en Conekta lo que creamos,
cuando una respuesta se pierde y hay que averiguar si el cargo existe.
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

#: Mismo host para sandbox y produccion. Conekta NO tiene un dominio de
#: pruebas: lo que distingue el ambiente es la llave, y la respuesta lo
#: reporta en el campo ``livemode``.
API_BASE: Final[str] = "https://api.conekta.io"

#: Version de la API fijada explicitamente. Sin esto, Conekta podria servir una
#: version distinta y cambiar el formato de respuesta sin aviso.
API_VERSION: Final[str] = "2.3.0"

#: Script oficial del tokenizador. El PAN se captura DENTRO de un iframe de
#: Conekta y nunca toca nuestro dominio; al servidor solo llega un token.
TOKENIZER_SCRIPT: Final[str] = "https://pay.conekta.com/v1.0/js/conekta-checkout.min.js"

#: Estados de una ORDEN que significan "el dinero esta confirmado".
_CONFIRMED_STATUSES: Final[frozenset[str]] = frozenset({"paid"})

#: Estados no terminales. ``pre_authorized`` y ``pending_confirmation`` NO son
#: cobros: el dinero esta reservado o en revision, no capturado. Tratarlos
#: como pagados entregaria la recarga contra un cargo que aun puede caerse.
_PENDING_STATUSES: Final[frozenset[str]] = frozenset(
    {"pending_payment", "pending_confirmation", "pre_authorized", "partially_paid"}
)

_DECLINED_STATUSES: Final[frozenset[str]] = frozenset(
    {"declined", "expired", "canceled", "voided", "charged_back", "refunded"}
)

#: Datos de contacto para una venta de mostrador.
#:
#: Conekta exige ``customer_info``, pero en una tienda de barrio el cliente
#: compra una recarga y se va: no da su correo ni su telefono, y pedirselos
#: para poder cobrarle seria absurdo. Se envian valores del COMERCIO, no
#: inventados sobre una persona: no se fabrica la identidad de nadie.
_EMAIL_MOSTRADOR: Final[str] = "mostrador@samycloud.mx"
_TELEFONO_MOSTRADOR: Final[str] = "+525500000000"


@dataclass(frozen=True, slots=True)
class _Ambiente:
    """Resultado de contrastar el ambiente real de Conekta con el configurado.

    ``problema`` vacio significa que todo cuadra. Se devuelve un objeto en vez
    de levantar porque esto lo consume ``check_health``, cuyo trabajo es
    *reportar* el estado, no interrumpir.
    """

    livemode: bool = False
    estado: ProviderStatus = ProviderStatus.READY
    problema: str = ""
    falta: tuple[str, ...] = ()


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

        latencia = int(response.elapsed.total_seconds() * 1000)

        # Autenticar no basta: hay que saber CONTRA QUE AMBIENTE. Conekta usa
        # el mismo dominio y el mismo prefijo de llave para pruebas y para
        # produccion, asi que un READY que no distinga los dos es justo el
        # aviso que no sirve. Se comprueba ademas que la llave de webhook
        # configurada sea de verdad una de las de esta cuenta: si no lo es,
        # todo webhook entrante se rechazara por firma invalida y las ordenes
        # con tarjeta se quedaran cobradas y sin confirmar.
        ambiente = self._describir_ambiente()
        if ambiente.problema:
            return ProviderHealth(
                status=ambiente.estado,
                detail=ambiente.problema,
                missing_requirements=ambiente.falta,
                latency_ms=latencia,
            )

        return ProviderHealth(
            status=ProviderStatus.READY,
            detail=(
                f"Conekta operativo en modo {self.mode} "
                f"(livemode={ambiente.livemode}). Llave de webhook verificada."
            ),
            latency_ms=latencia,
        )

    def _describir_ambiente(self) -> "_Ambiente":
        """Contrasta el ambiente real y la llave de webhook con lo configurado."""
        try:
            with self._client() as client:
                respuesta = client.get("/webhook_keys")
        except httpx.HTTPError as exc:
            return _Ambiente(
                estado=ProviderStatus.DEGRADED,
                problema=f"No se pudo confirmar el ambiente de Conekta: {exc}",
            )

        if respuesta.status_code >= 400:
            return _Ambiente(
                estado=ProviderStatus.DEGRADED,
                problema=(
                    f"Conekta respondio {respuesta.status_code} al consultar las "
                    "llaves de webhook; no se puede confirmar el ambiente."
                ),
            )

        llaves = (self._parse(respuesta).get("data") or []) if respuesta.content else []
        if not llaves:
            return _Ambiente(
                estado=ProviderStatus.NOT_CONFIGURED,
                problema=(
                    "La cuenta de Conekta no tiene ninguna llave de webhook. "
                    "Sin ella no se puede verificar la firma de los webhooks, "
                    "asi que un cobro con tarjeta nunca quedaria confirmado."
                ),
                falta=("Llave de webhook creada en Conekta (POST /webhook_keys)",),
            )

        esperado_produccion = self.mode == ProviderMode.PRODUCTION
        modos = {bool(k.get("livemode")) for k in llaves}
        if modos != {esperado_produccion}:
            return _Ambiente(
                estado=ProviderStatus.DEGRADED,
                problema=(
                    f"SAMY Cloud esta configurado en modo {self.mode} pero la "
                    f"cuenta de Conekta reporta livemode={sorted(modos)}. "
                    "Revisa CONEKTA_PRIVATE_KEY y CONEKTA_MODE antes de cobrar."
                ),
            )

        # Comparacion normalizada: dos PEM iguales pueden diferir en saltos de
        # linea finales segun como se hayan guardado.
        def _normalizar(pem: str) -> str:
            return "".join((pem or "").split())

        configurada = _normalizar(self.config.webhook_public_key)
        if configurada not in {_normalizar(str(k.get("public_key") or "")) for k in llaves}:
            return _Ambiente(
                estado=ProviderStatus.DEGRADED,
                problema=(
                    "La llave de webhook configurada no coincide con ninguna de "
                    "las de esta cuenta de Conekta. Los webhooks se rechazarian "
                    "por firma invalida. Revisa CONEKTA_WEBHOOK_PUBLIC_KEY."
                ),
            )

        return _Ambiente(livemode=esperado_produccion)

    # -- cobro -------------------------------------------------------------

    def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Crea la orden en Conekta y la cobra con el token de la tarjeta.

        El numero de tarjeta NUNCA pasa por aqui. Lo captura el tokenizador de
        Conekta dentro de su propio iframe y lo que llega a este metodo es un
        token de un solo uso, que ademas caduca a los 10 minutos. Es lo que
        mantiene el alcance PCI DSS en SAQ A.

        Sin token no se cobra: se levanta en vez de crear una orden vacia que
        quedaria colgada en Conekta sin corresponder a nada.
        """
        self.ensure_ready()

        token = (intent.card_token or "").strip()
        if not token:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    "Falta el token de la tarjeta. El cobro con tarjeta exige "
                    "tokenizar primero en el navegador; sin token no se cobra."
                ),
            )

        # ``customer_info`` es obligatorio para Conekta. Se envia lo minimo
        # necesario: nada de datos del cliente que no hagan falta para cobrar.
        payload: dict[str, Any] = {
            "currency": intent.amount.currency,
            "customer_info": {
                "name": (intent.customer_name or "Cliente de mostrador")[:120],
                "email": intent.customer_email or _EMAIL_MOSTRADOR,
                "phone": intent.customer_phone or _TELEFONO_MOSTRADOR,
            },
            "line_items": [
                {
                    "name": intent.description[:120] or "Operacion SAMY Cloud",
                    "unit_price": intent.amount.cents,
                    "quantity": 1,
                }
            ],
            "charges": [
                {
                    "amount": intent.amount.cents,
                    "payment_method": {"type": "card", "token_id": token},
                }
            ],
            # Solo valores escalares: Conekta rechaza metadata anidada. Sirve
            # para reencontrar en Conekta lo que creamos aqui cuando una
            # respuesta se pierde.
            "metadata": {
                "samy_order_id": str(intent.order_id),
                "samy_folio": intent.folio,
                "samy_store_id": str(intent.store_id),
                "samy_attempt_id": intent.idempotency_key,
            },
        }

        try:
            with self._client() as client:
                response = client.post("/orders", json=payload)
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

        self._verificar_ambiente(data)

        status = str(data.get("payment_status") or "")
        checkout = data.get("checkout") or {}

        return PaymentResult(
            outcome=self._map_outcome(status),
            provider_reference=str(data.get("id", "")),
            provider_mode=str(self.mode),
            checkout_url=str(checkout.get("url", "")),
            raw_response=self._safe(data),
        )

    def _verificar_ambiente(self, data: dict[str, Any]) -> None:
        """Comprueba que la respuesta venga del ambiente que creemos usar.

        Conekta usa el mismo dominio y el mismo prefijo de llave para pruebas
        y produccion; lo unico que distingue el ambiente es el campo
        ``livemode``. Sin esta comprobacion, una llave de produccion pegada
        por error en la configuracion de sandbox cobraria dinero real mientras
        toda la interfaz dice "pruebas".
        """
        livemode = data.get("livemode")
        if livemode is None:
            return

        es_produccion = self.mode == ProviderMode.PRODUCTION
        if bool(livemode) != es_produccion:
            log.error(
                "conekta_ambiente_no_coincide",
                configurado=str(self.mode),
                livemode=livemode,
            )
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    f"SAMY Cloud esta configurado en modo {self.mode} pero la "
                    f"llave de Conekta es de {'produccion' if livemode else 'pruebas'}. "
                    "Se detiene la operacion: revisa CONEKTA_PRIVATE_KEY y "
                    "CONEKTA_MODE antes de continuar."
                ),
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

        # data.object NO siempre es la orden, y ahi estaba el error.
        #
        #   * "order.*"     -> data.object ES la orden. Su id es el ord_... que
        #     guardamos en el intento de cobro; el estado viene en
        #     "payment_status".
        #   * "charge.*"    -> data.object es el CARGO. Su id es un ide de
        #     cargo, no de orden; el estado viene en "status".
        #   * "charge.chargeback.*" -> data.object es el CONTRACARGO
        #     ("object": "chargeback"), con su propio id, su charge_id y un
        #     "status" de disputa (action_required, under_review, won, lost).
        #
        # Lo que tienen en comun los objetos anidados es que todos llevan
        # "order_id". Esa es la regla, y por eso se busca primero: si el objeto
        # apunta a una orden, esa es la referencia; si no, el objeto es la
        # orden. Asi no hay que ir agregando un caso por cada tipo de evento
        # que Conekta invente.
        #
        # Antes se leia "id" siempre, asi que TODO evento que no fuera de orden
        # terminaba en "orden no encontrada": entraba, se acusaba recibo con un
        # 200 y no se aplicaba a nada. Silencioso, y justo en la parte que
        # devuelve dinero.
        #
        # Ojo con el desenlace de un contracargo: sus estados son de disputa,
        # no de pago, asi que _map_outcome los deja en UNKNOWN a proposito. Eso
        # NO cambia la orden; la manda a conciliacion, que le pregunta a
        # Conekta cual es el estado real del pago. Es lo correcto: un
        # contracargo abierto todavia se puede ganar, y dar por perdido el
        # dinero antes de tiempo seria inventar un desenlace.
        anidado = bool(data_object.get("order_id"))
        if anidado:
            referencia = str(data_object.get("order_id") or "")
            estado = str(data_object.get("status") or "")
        else:
            referencia = str(data_object.get("id") or "")
            estado = str(data_object.get("payment_status") or "")

        return WebhookEvent(
            event_id=str(body.get("id", "")),
            event_type=str(body.get("type", "")),
            provider_reference=referencia,
            outcome=self._map_outcome(estado),
            amount=Money(int(amount_cents)) if amount_cents is not None else None,
            raw_payload=self._safe(body),
        )

    # -- internos ------------------------------------------------------------

    def _client(self) -> httpx.Client:
        # Bearer y no Basic: es lo que declara la documentacion de
        # autenticacion de Conekta y lo que usan todos sus SDK. (Su propio
        # ejemplo de cURL muestra Basic, pero es la excepcion.)
        return httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {self.config.private_key}",
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
