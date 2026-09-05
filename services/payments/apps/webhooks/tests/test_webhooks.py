"""Pruebas del webhook de pagos.

Un webhook es una instruccion de un desconocido para marcar dinero como
cobrado. Estas pruebas comprueban las tres cosas que impiden que eso sea un
agujero: firma valida obligatoria, deduplicacion, y que un monto que no
coincide no confirme nada.

La firma se genera con una llave RSA creada en la propia prueba. Asi se
verifica el mecanismo REAL de verificacion, no una version simplificada: si
alguien debilitara ``verify_webhook``, estas pruebas fallarian.
"""

from __future__ import annotations

import base64
import json
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.orders.models import Order
from apps.payments.models import PaymentAttempt, PaymentAttemptStatus
from apps.webhooks.models import ReceivedWebhook, WebhookProcessingResult
from samy_common.states import OrderState

TIENDA = uuid.uuid4()
ORGANIZACION = uuid.uuid4()
CAJERO = uuid.uuid4()

#: Par de llaves de prueba. Se genera una vez por modulo: RSA de 2048 bits es
#: lento y hacerlo en cada prueba multiplicaria el tiempo sin aportar nada.
_LLAVE_PRIVADA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_LLAVE_PUBLICA_PEM = (
    _LLAVE_PRIVADA.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)


def _firmar(cuerpo: bytes) -> str:
    """Firma como lo hace Conekta: RSA-SHA256 sobre el cuerpo crudo, en base64."""
    firma = _LLAVE_PRIVADA.sign(cuerpo, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(firma).decode()


def _evento(
    *, referencia: str, monto_cents: int, event_id: str = "", estado: str = "paid"
) -> bytes:
    """Cuerpo de un evento de Conekta, con la forma que documenta el proveedor."""
    return json.dumps(
        {
            "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
            "object": "event",
            "type": "order.paid",
            "livemode": False,
            "data": {
                "object": {
                    "id": referencia,
                    "payment_status": estado,
                    "amount": monto_cents,
                }
            },
        }
    ).encode()


class WebhookBase(TestCase):
    """Monta una orden con su intento, como quedaria tras iniciar un cobro."""

    def setUp(self) -> None:
        cache.clear()

        self.order = Order.objects.create(
            folio="TEST-260905-AAAA",
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            created_by_id=CAJERO,
            created_by_email="cajero@negocio.mx",
            service_kind="TOPUP",
            description="Recarga de prueba",
            currency="MXN",
            base_cents=5000,
            commission_cents=300,
            total_cents=5300,
            state=OrderState.PAYMENT_PENDING,
        )
        self.attempt = PaymentAttempt.objects.create(
            order=self.order,
            provider_slug="conekta",
            provider_mode="SANDBOX",
            method="CARD",
            amount_cents=5300,
            currency="MXN",
            status=PaymentAttemptStatus.AWAITING_CUSTOMER,
            provider_reference="ord_prueba_123",
            idempotency_key=f"{self.order.id}:abc123",
        )
        self.url = reverse("webhooks:conekta")

    def _enviar(self, cuerpo: bytes, firma: str | None = None):
        return self.client.post(
            self.url,
            data=cuerpo,
            content_type="application/json",
            headers={"digest": firma if firma is not None else _firmar(cuerpo)},
        )


@override_settings(
    CONEKTA_PRIVATE_KEY="key_de_prueba",
    CONEKTA_PUBLIC_KEY="key_publica_de_prueba",
    CONEKTA_WEBHOOK_PUBLIC_KEY=_LLAVE_PUBLICA_PEM,
)
class WebhookFirmaTests(WebhookBase):
    """10 y 11. Webhook valido y webhook invalido."""

    def test_webhook_sin_firma_se_rechaza(self) -> None:
        """Sin cabecera de firma no se procesa nada. Es la puerta principal."""
        cuerpo = _evento(referencia="ord_prueba_123", monto_cents=5300)

        respuesta = self.client.post(
            self.url, data=cuerpo, content_type="application/json"
        )

        self.assertEqual(respuesta.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderState.PAYMENT_PENDING)

    def test_webhook_con_firma_invalida_se_rechaza(self) -> None:
        """Una firma que no valida no marca nada como pagado.

        Es el ataque directo: alguien que descubre la URL y envia un evento
        diciendo "esta orden ya se pago" para llevarse una recarga gratis.
        """
        cuerpo = _evento(referencia="ord_prueba_123", monto_cents=5300)
        firma_falsa = base64.b64encode(b"esto no es una firma valida").decode()

        respuesta = self._enviar(cuerpo, firma=firma_falsa)

        self.assertEqual(respuesta.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderState.PAYMENT_PENDING)
        # Un evento rechazado NO se guarda: esta tabla es evidencia de lo que
        # el proveedor dijo, no de intentos de suplantacion.
        self.assertFalse(ReceivedWebhook.objects.exists())

    def test_cuerpo_alterado_despues_de_firmar_se_rechaza(self) -> None:
        """La firma cubre el cuerpo entero: cambiar un centavo la invalida."""
        original = _evento(referencia="ord_prueba_123", monto_cents=5300)
        firma = _firmar(original)
        alterado = original.replace(b'"amount": 5300', b'"amount": 1')

        respuesta = self._enviar(alterado, firma=firma)

        self.assertEqual(respuesta.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderState.PAYMENT_PENDING)

    def test_webhook_valido_marca_la_orden_como_pagada(self) -> None:
        cuerpo = _evento(referencia="ord_prueba_123", monto_cents=5300)

        respuesta = self._enviar(cuerpo)

        self.assertEqual(respuesta.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderState.PAID)
        self.assertIsNotNone(self.order.paid_at)

        registro = ReceivedWebhook.objects.get()
        self.assertEqual(registro.result, WebhookProcessingResult.APPLIED)

    def test_el_pago_dispara_el_evento_que_ejecuta_la_recarga(self) -> None:
        """El outbox es lo que conecta "se pago" con "ejecuta la recarga"."""
        from apps.outbox.models import OutboxEvent

        self._enviar(_evento(referencia="ord_prueba_123", monto_cents=5300))

        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type="order.paid", aggregate_id=self.order.id
            ).exists()
        )

    def test_un_monto_menor_no_marca_la_orden_como_pagada(self) -> None:
        """Cobrar $1 no entrega una recarga de $53."""
        cuerpo = _evento(referencia="ord_prueba_123", monto_cents=100)

        respuesta = self._enviar(cuerpo)

        self.assertEqual(respuesta.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderState.PAYMENT_PENDING)
        self.assertEqual(
            ReceivedWebhook.objects.get().result, WebhookProcessingResult.REJECTED
        )

    def test_evento_de_una_orden_desconocida_no_rompe_nada(self) -> None:
        cuerpo = _evento(referencia="ord_que_no_existe", monto_cents=5300)

        respuesta = self._enviar(cuerpo)

        # 200 para que el proveedor deje de reintentar, pero queda anotado.
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            ReceivedWebhook.objects.get().result,
            WebhookProcessingResult.ORDER_NOT_FOUND,
        )


@override_settings(
    CONEKTA_PRIVATE_KEY="key_de_prueba",
    CONEKTA_PUBLIC_KEY="key_publica_de_prueba",
    CONEKTA_WEBHOOK_PUBLIC_KEY=_LLAVE_PUBLICA_PEM,
)
class WebhookDuplicadoTests(WebhookBase):
    """12. Webhook duplicado: ni doble cobro ni doble recarga."""

    def test_el_mismo_evento_dos_veces_solo_se_procesa_una(self) -> None:
        from apps.outbox.models import OutboxEvent

        cuerpo = _evento(
            referencia="ord_prueba_123", monto_cents=5300, event_id="evt_repetido"
        )

        primera = self._enviar(cuerpo)
        segunda = self._enviar(cuerpo)

        self.assertEqual(primera.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(segunda.json()["status"], "duplicate_ignored")

        # Un solo registro, y sobre todo: UN SOLO evento de "orden pagada".
        # Si hubiera dos, la recarga se ejecutaria dos veces.
        self.assertEqual(ReceivedWebhook.objects.count(), 1)
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type="order.paid", aggregate_id=self.order.id
            ).count(),
            1,
        )

    def test_el_duplicado_se_detecta_aunque_se_pierda_la_cache(self) -> None:
        """La cache es la primera barrera; la base es la que garantiza.

        Se simula un reinicio de Redis limpiando la cache entre las dos
        entregas. Sin la tabla, el segundo evento pareceria nuevo.
        """
        from apps.outbox.models import OutboxEvent

        cuerpo = _evento(
            referencia="ord_prueba_123", monto_cents=5300, event_id="evt_tras_reinicio"
        )

        self._enviar(cuerpo)
        cache.clear()  # se cayo Redis
        segunda = self._enviar(cuerpo)

        self.assertEqual(segunda.json()["status"], "duplicate_ignored")
        self.assertEqual(ReceivedWebhook.objects.count(), 1)
        self.assertEqual(
            OutboxEvent.objects.filter(event_type="order.paid").count(), 1
        )

    def test_dos_eventos_distintos_de_la_misma_orden_no_pagan_dos_veces(self) -> None:
        """Aunque los identificadores difieran, la orden ya pagada no se repaga.

        Es la segunda capa: ``confirm_payment`` es idempotente por si misma.
        """
        from apps.outbox.models import OutboxEvent

        self._enviar(
            _evento(
                referencia="ord_prueba_123", monto_cents=5300, event_id="evt_uno"
            )
        )
        self._enviar(
            _evento(
                referencia="ord_prueba_123", monto_cents=5300, event_id="evt_dos"
            )
        )

        self.assertEqual(ReceivedWebhook.objects.count(), 2)
        # Dos eventos entraron, pero solo un "order.paid" salio.
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type="order.paid", aggregate_id=self.order.id
            ).count(),
            1,
        )
