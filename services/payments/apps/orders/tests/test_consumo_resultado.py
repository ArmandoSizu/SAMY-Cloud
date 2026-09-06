"""Cierre de la orden a partir del resultado que publica el servicio ejecutor.

Este archivo protege el eslabon que faltaba en la cadena del dinero. Recargas
ejecutaba y publicaba ``fulfillment.result``; Pagos sabia aplicarlo; pero nada
leia el stream. Una recarga entregada de verdad por el proveedor dejaba la
orden en ``PAID`` indefinidamente y el comprobante decia "PAGADA - EN PROCESO"
a un cliente que ya tenia su saldo.

Lo que se comprueba aqui:

* que un resultado exitoso cierre la orden pasando por ``PROCESSING``;
* que un fallo tras el cobro deje la orden en ``REFUND_PENDING`` sola;
* que un evento repetido no vuelva a cerrar ni dispare un segundo reembolso;
* que un resultado indeterminado NO invente un desenlace.
"""

from __future__ import annotations

import uuid

from django.test import TestCase

from apps.orders import services
from apps.orders.consumers import _aplicar_resultado
from apps.orders.models import Order, ServiceKind
from samy_common.events.bus import Event
from samy_common.money import Money
from samy_common.states import OrderState

TIENDA = uuid.uuid4()
ORGANIZACION = uuid.uuid4()
CAJERO = uuid.uuid4()


def _orden_pagada() -> Order:
    order = services.create_order(
        organization_id=ORGANIZACION,
        store_id=TIENDA,
        store_code="CENTRO",
        created_by_id=CAJERO,
        created_by_email="cajero@negocio.mx",
        service_kind=ServiceKind.TOPUP,
        description="Recarga de prueba",
        base_amount=Money.parse("89.85"),
    )
    return order.transition(OrderState.PAID, reason="Efectivo recibido.")


def _evento(order: Order, **extra) -> Event:
    payload = {
        "fulfillment_id": str(uuid.uuid4()),
        "order_id": str(order.id),
        "succeeded": True,
        "pending_review": False,
        "provider_reference": "179162",
        "operator_reference": "",
        "failure_reason": "",
        "state": "SUCCEEDED",
    }
    payload.update(extra)
    return Event(
        event_id=str(uuid.uuid4()),
        event_type="fulfillment.result",
        aggregate_type="TopupFulfillment",
        aggregate_id=payload["fulfillment_id"],
        correlation_id="corr-1",
        payload=payload,
    )


class ResultadoExitosoTests(TestCase):
    def test_una_entrega_exitosa_cierra_la_orden(self) -> None:
        order = _orden_pagada()

        aplicado = _aplicar_resultado(_evento(order))

        order.refresh_from_db()
        self.assertTrue(aplicado)
        self.assertEqual(order.state_enum, OrderState.SUCCESS)

    def test_la_orden_pasa_por_en_proceso_y_deja_rastro(self) -> None:
        """No hay atajo PAID -> SUCCESS: el recorrido queda registrado."""
        order = _orden_pagada()

        _aplicar_resultado(_evento(order))

        estados = list(
            order.events.order_by("created_at").values_list("new_state", flat=True)
        )
        self.assertIn(OrderState.PROCESSING.value, estados)
        self.assertIn(OrderState.SUCCESS.value, estados)
        self.assertLess(
            estados.index(OrderState.PROCESSING.value),
            estados.index(OrderState.SUCCESS.value),
        )

    def test_guarda_la_referencia_del_proveedor(self) -> None:
        order = _orden_pagada()

        _aplicar_resultado(_evento(order))

        ultima = order.events.order_by("-created_at").first()
        self.assertIn("179162", str(ultima.metadata))


class ResultadoFallidoTests(TestCase):
    def test_un_fallo_tras_el_cobro_deja_el_reembolso_pendiente(self) -> None:
        """Se cobro y no se entrego: la devolucion se dispara sola."""
        order = _orden_pagada()

        _aplicar_resultado(
            _evento(
                order,
                succeeded=False,
                state="FAILED",
                failure_reason="El operador rechazo el numero.",
            )
        )

        order.refresh_from_db()
        self.assertEqual(order.state_enum, OrderState.REFUND_PENDING)

    def test_conserva_el_motivo_del_fallo(self) -> None:
        order = _orden_pagada()

        _aplicar_resultado(
            _evento(
                order,
                succeeded=False,
                state="FAILED",
                failure_reason="El operador rechazo el numero.",
            )
        )

        motivos = " ".join(order.events.values_list("reason", flat=True))
        self.assertIn("rechazo el numero", motivos)


class IdempotenciaTests(TestCase):
    """La entrega del bus es 'al menos una vez'. Repetir no puede cobrar dos veces."""

    def test_un_evento_repetido_no_vuelve_a_cerrar_la_orden(self) -> None:
        order = _orden_pagada()
        evento = _evento(order)

        self.assertTrue(_aplicar_resultado(evento))
        self.assertFalse(_aplicar_resultado(evento))

        order.refresh_from_db()
        self.assertEqual(order.state_enum, OrderState.SUCCESS)

    def test_un_fallo_repetido_no_dispara_dos_reembolsos(self) -> None:
        order = _orden_pagada()
        evento = _evento(order, succeeded=False, state="FAILED")

        _aplicar_resultado(evento)
        transiciones_tras_el_primero = order.events.count()
        _aplicar_resultado(evento)

        order.refresh_from_db()
        self.assertEqual(order.state_enum, OrderState.REFUND_PENDING)
        self.assertEqual(order.events.count(), transiciones_tras_el_primero)


class ResultadoIndeterminadoTests(TestCase):
    def test_un_resultado_indeterminado_no_inventa_desenlace(self) -> None:
        """Ni exito ni fracaso: se queda en proceso hasta saberlo de verdad."""
        order = _orden_pagada()

        aplicado = _aplicar_resultado(
            _evento(order, succeeded=False, pending_review=True, state="UNDER_REVIEW")
        )

        order.refresh_from_db()
        self.assertFalse(aplicado)
        self.assertEqual(order.state_enum, OrderState.PROCESSING)

    def test_despues_la_conciliacion_puede_cerrarla(self) -> None:
        order = _orden_pagada()
        _aplicar_resultado(
            _evento(order, succeeded=False, pending_review=True, state="UNDER_REVIEW")
        )

        order.refresh_from_db()
        aplicado = _aplicar_resultado(_evento(order))

        order.refresh_from_db()
        self.assertTrue(aplicado)
        self.assertEqual(order.state_enum, OrderState.SUCCESS)


class EventosInvalidosTests(TestCase):
    def test_un_evento_sin_orden_no_revienta(self) -> None:
        order = _orden_pagada()
        evento = _evento(order, order_id=None)

        self.assertFalse(_aplicar_resultado(evento))

    def test_una_orden_inexistente_no_revienta(self) -> None:
        order = _orden_pagada()
        evento = _evento(order, order_id=str(uuid.uuid4()))

        self.assertFalse(_aplicar_resultado(evento))

    def test_no_cierra_una_orden_que_no_esta_pagada(self) -> None:
        """Un resultado de entrega sobre una orden sin cobrar es un sinsentido."""
        order = services.create_order(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            store_code="CENTRO",
            created_by_id=CAJERO,
            created_by_email="cajero@negocio.mx",
            service_kind=ServiceKind.TOPUP,
            description="Recarga sin pagar",
            base_amount=Money.parse("50.00"),
        )

        aplicado = _aplicar_resultado(_evento(order))

        order.refresh_from_db()
        self.assertFalse(aplicado)
        self.assertEqual(order.state_enum, OrderState.CREATED)
