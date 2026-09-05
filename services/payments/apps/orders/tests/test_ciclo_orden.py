"""Pruebas del ciclo de vida de una orden y de la maquina de estados.

La regla que protege este archivo:

    PAYMENT_PENDING -> PAID -> PROCESSING -> SUCCESS

y sobre todo que **no exista ningun atajo** desde antes de PAID hasta la
ejecucion del servicio. Esa garantia no vive en un ``if``: vive en la ausencia
de aristas en la maquina de estados, que es mucho mas dificil de romper por
accidente.
"""

from __future__ import annotations

import uuid

from django.test import TestCase

from apps.orders import services
from apps.orders.models import Order, ServiceKind
from apps.payments.models import PaymentAttempt, PaymentAttemptStatus
from samy_common.money import Money
from samy_common.states import OrderState, can_transition

TIENDA = uuid.uuid4()
ORGANIZACION = uuid.uuid4()
CAJERO = uuid.uuid4()


def _crear_orden(**extra) -> Order:
    parametros = {
        "organization_id": ORGANIZACION,
        "store_id": TIENDA,
        "store_code": "CENTRO",
        "created_by_id": CAJERO,
        "created_by_email": "cajero@negocio.mx",
        "service_kind": ServiceKind.TOPUP,
        "description": "Recarga de prueba",
        "base_amount": Money.parse("50.00"),
    }
    parametros.update(extra)
    return services.create_order(**parametros)


class MaquinaDeEstadosTests(TestCase):
    """La regla del dinero, comprobada en la propia maquina de estados."""

    def test_no_existe_camino_de_creada_a_en_proceso(self) -> None:
        """Sin pagar no se ejecuta. No hay arista que lo permita."""
        self.assertFalse(
            can_transition(OrderState.CREATED, OrderState.PROCESSING)
        )

    def test_no_existe_camino_de_pago_pendiente_a_en_proceso(self) -> None:
        """El caso peligroso de verdad: el cobro se inicio pero no se confirmo."""
        self.assertFalse(
            can_transition(OrderState.PAYMENT_PENDING, OrderState.PROCESSING)
        )

    def test_no_existe_camino_de_creada_a_exitosa(self) -> None:
        self.assertFalse(can_transition(OrderState.CREATED, OrderState.SUCCESS))

    def test_el_unico_camino_a_la_ejecucion_pasa_por_pagada(self) -> None:
        self.assertTrue(can_transition(OrderState.PAID, OrderState.PROCESSING))
        self.assertTrue(can_transition(OrderState.PROCESSING, OrderState.SUCCESS))

    def test_una_orden_exitosa_no_retrocede(self) -> None:
        """Un estado terminal es terminal: la venta no se reabre sola."""
        for destino in (
            OrderState.CREATED,
            OrderState.PAYMENT_PENDING,
            OrderState.PAID,
            OrderState.PROCESSING,
        ):
            self.assertFalse(
                can_transition(OrderState.SUCCESS, destino),
                f"SUCCESS no deberia poder volver a {destino}",
            )


class CreacionDeOrdenTests(TestCase):
    """13. Creacion de orden y calculo del dinero."""

    def test_la_orden_nace_creada_y_con_su_comision_congelada(self) -> None:
        orden = _crear_orden()

        self.assertEqual(orden.state, OrderState.CREATED)
        self.assertEqual(orden.base_cents, 5000)
        # El total SIEMPRE cuadra: base + comision, sin redondeos perdidos.
        self.assertEqual(
            orden.total_cents, orden.base_cents + orden.commission_cents
        )
        self.assertTrue(orden.folio.startswith("CENTRO-"))

    def test_el_reparto_de_la_comision_suma_exactamente_la_comision(self) -> None:
        """Ni un centavo se pierde ni se inventa en el reparto."""
        orden = _crear_orden(base_amount=Money.parse("333.33"))
        entrada = orden.commission_entry

        self.assertEqual(
            entrada.store_share_cents
            + entrada.platform_share_cents
            + entrada.provider_share_cents,
            entrada.commission_cents,
        )

    def test_la_misma_clave_de_idempotencia_no_crea_dos_ordenes(self) -> None:
        """El cajero nervioso que pulsa dos veces no genera dos ventas."""
        clave = f"prueba-{uuid.uuid4().hex}"

        primera = _crear_orden(idempotency_key=clave)
        segunda = _crear_orden(idempotency_key=clave)

        self.assertEqual(primera.id, segunda.id)
        self.assertEqual(primera.folio, segunda.folio)
        self.assertEqual(Order.objects.count(), 1)

    def test_dos_tiendas_pueden_usar_la_misma_clave_sin_pisarse(self) -> None:
        """La idempotencia es POR TIENDA: son negocios distintos."""
        clave = f"prueba-{uuid.uuid4().hex}"
        otra_tienda = uuid.uuid4()

        _crear_orden(idempotency_key=clave)
        _crear_orden(idempotency_key=clave, store_id=otra_tienda)

        self.assertEqual(Order.objects.count(), 2)


class ConfirmacionDePagoTests(TestCase):
    """14. Confirmacion del pago: la unica puerta hacia la ejecucion."""

    def setUp(self) -> None:
        self.orden = _crear_orden()
        self.orden = self.orden.transition(
            OrderState.PAYMENT_PENDING, reason="Cobro iniciado."
        )
        self.intento = PaymentAttempt.objects.create(
            order=self.orden,
            provider_slug="conekta",
            provider_mode="SANDBOX",
            method="CARD",
            amount_cents=self.orden.total_cents,
            currency="MXN",
            status=PaymentAttemptStatus.AWAITING_CUSTOMER,
            provider_reference="ord_x",
            idempotency_key=f"{self.orden.id}:x",
        )

    def test_confirmar_el_pago_deja_la_orden_pagada(self) -> None:
        resultado = services.confirm_payment(
            order=self.orden,
            attempt=self.intento,
            provider_reference="ord_x",
            source="prueba",
        )

        self.assertEqual(resultado.state, OrderState.PAID)
        self.assertIsNotNone(resultado.paid_at)

    def test_confirmar_dos_veces_no_emite_dos_eventos(self) -> None:
        """Es lo que impide que un webhook reenviado recargue dos veces."""
        from apps.outbox.models import OutboxEvent

        services.confirm_payment(
            order=self.orden,
            attempt=self.intento,
            provider_reference="ord_x",
            source="webhook",
        )
        self.orden.refresh_from_db()
        services.confirm_payment(
            order=self.orden,
            attempt=self.intento,
            provider_reference="ord_x",
            source="webhook_reenviado",
        )

        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type="order.paid", aggregate_id=self.orden.id
            ).count(),
            1,
        )

    def test_un_monto_insuficiente_no_confirma_el_pago(self) -> None:
        from samy_common.providers.exceptions import ProviderPermanentError

        with self.assertRaises(ProviderPermanentError):
            services.confirm_payment(
                order=self.orden,
                attempt=self.intento,
                provider_reference="ord_x",
                source="prueba",
                amount_received=Money.parse("1.00"),
            )

        self.orden.refresh_from_db()
        self.assertEqual(self.orden.state, OrderState.PAYMENT_PENDING)

    def test_una_recarga_no_se_cierra_sola_al_pagarse(self) -> None:
        """Pagar NO es entregar.

        Una recarga queda en PAID esperando la confirmacion del operador. Solo
        una venta propia del comercio, que no tiene nada que entregar, se
        completa en el acto.
        """
        resultado = services.confirm_payment(
            order=self.orden,
            attempt=self.intento,
            provider_reference="ord_x",
            source="prueba",
        )

        self.assertEqual(resultado.state, OrderState.PAID)
        self.assertNotEqual(resultado.state, OrderState.SUCCESS)


class FalloTrasCobrarTests(TestCase):
    """Que pasa cuando se cobro y la recarga fallo."""

    def setUp(self) -> None:
        self.orden = _crear_orden()
        self.orden = self.orden.transition(OrderState.PAYMENT_PENDING, reason="")
        self.orden = self.orden.transition(OrderState.PAID, reason="")
        self.orden = self.orden.transition(OrderState.PROCESSING, reason="")

    def test_si_la_recarga_falla_el_reembolso_se_dispara_solo(self) -> None:
        """El cliente pago por algo que no recibio. No queda a criterio de nadie."""
        resultado = services.record_fulfillment_result(
            order=self.orden,
            succeeded=False,
            reason="El operador rechazo la recarga.",
        )

        self.assertEqual(resultado.state, OrderState.REFUND_PENDING)

    def test_el_fallo_emite_el_evento_de_reembolso(self) -> None:
        from apps.outbox.models import OutboxEvent

        services.record_fulfillment_result(
            order=self.orden, succeeded=False, reason="Fallo del operador."
        )

        evento = OutboxEvent.objects.filter(
            event_type="order.refund_required", aggregate_id=self.orden.id
        ).first()
        self.assertIsNotNone(evento)
        # El monto a devolver es el TOTAL cobrado, comision incluida: el
        # cliente entrego ese dinero y no recibio nada.
        self.assertEqual(evento.payload["amount_cents"], self.orden.total_cents)

    def test_una_recarga_exitosa_cierra_la_orden(self) -> None:
        resultado = services.record_fulfillment_result(
            order=self.orden, succeeded=True, provider_reference="TX-1"
        )
        self.assertEqual(resultado.state, OrderState.SUCCESS)


class TokenDeTarjetaTests(TestCase):
    """El PAN no puede entrar al sistema ni por error."""

    def test_el_serializador_rechaza_algo_que_parece_una_tarjeta(self) -> None:
        from apps.orders.serializers import StartPaymentSerializer

        serializador = StartPaymentSerializer(
            data={
                "store_id": str(TIENDA),
                "actor_id": str(CAJERO),
                "method": "CARD",
                "card_token": "4242424242424242",
            }
        )

        self.assertFalse(serializador.is_valid())
        self.assertIn("card_token", serializador.errors)

    def test_el_serializador_acepta_un_token_real(self) -> None:
        from apps.orders.serializers import StartPaymentSerializer

        serializador = StartPaymentSerializer(
            data={
                "store_id": str(TIENDA),
                "actor_id": str(CAJERO),
                "method": "CARD",
                "card_token": "tok_test_visa_4242",
            }
        )

        self.assertTrue(serializador.is_valid(), serializador.errors)
        self.assertEqual(
            serializador.validated_data["card_token"], "tok_test_visa_4242"
        )

    def test_el_efectivo_no_necesita_token(self) -> None:
        from apps.orders.serializers import StartPaymentSerializer

        serializador = StartPaymentSerializer(
            data={
                "store_id": str(TIENDA),
                "actor_id": str(CAJERO),
                "method": "CASH",
            }
        )

        self.assertTrue(serializador.is_valid(), serializador.errors)
        self.assertEqual(serializador.validated_data["card_token"], "")


class AislamientoDeTiendasTests(TestCase):
    """Aislamiento: una tienda no ve las ordenes de otra."""

    def test_las_ordenes_quedan_atadas_a_su_tienda(self) -> None:
        tienda_a, tienda_b = uuid.uuid4(), uuid.uuid4()

        _crear_orden(store_id=tienda_a)
        _crear_orden(store_id=tienda_b)

        self.assertEqual(Order.objects.filter(store_id=tienda_a).count(), 1)
        self.assertEqual(Order.objects.filter(store_id=tienda_b).count(), 1)
        self.assertFalse(
            Order.objects.filter(store_id=tienda_a)
            .filter(store_id=tienda_b)
            .exists()
        )
