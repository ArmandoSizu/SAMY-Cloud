"""Pruebas de la maquina de estados.

La prueba mas importante del proyecto es ``test_no_se_puede_ejecutar_sin_pagar``:
verifica que la regla del dinero esta impuesta por la estructura del grafo de
transiciones, no por un ``if`` que alguien pueda olvidar en un camino nuevo.
"""

from __future__ import annotations

import pytest

from samy_common.states import (
    FULFILLMENT_TRANSITIONS,
    MONEY_HELD_STATES,
    ORDER_TRANSITIONS,
    TERMINAL_ORDER_STATES,
    FulfillmentState,
    IllegalTransition,
    OrderState,
    assert_transition,
    can_transition,
)


class TestReglaDelDinero:
    """PRIMERO SE CONFIRMA EL PAGO. DESPUES SE EJECUTA EL SERVICIO."""

    @pytest.mark.parametrize(
        "estado_sin_pagar",
        [OrderState.CREATED, OrderState.PAYMENT_PENDING],
    )
    def test_no_se_puede_ejecutar_sin_pagar(self, estado_sin_pagar):
        """LA prueba del proyecto.

        Desde un estado no pagado NO existe ninguna arista hacia PROCESSING.
        Esta ausencia es la implementacion literal de la regla del dinero.
        """
        assert not can_transition(estado_sin_pagar, OrderState.PROCESSING)

        with pytest.raises(IllegalTransition):
            assert_transition(estado_sin_pagar, OrderState.PROCESSING)

    def test_solo_paid_abre_la_puerta(self):
        assert can_transition(OrderState.PAID, OrderState.PROCESSING)

    def test_ningun_estado_salta_directo_a_success(self):
        """Solo se llega a SUCCESS pasando por PROCESSING (o por revision)."""
        origenes_validos = {OrderState.PROCESSING, OrderState.UNDER_REVIEW}
        for origen, destinos in ORDER_TRANSITIONS.items():
            if OrderState.SUCCESS in destinos:
                assert origen in origenes_validos, (
                    f"{origen} puede saltar a SUCCESS sin pasar por PROCESSING"
                )


class TestTransicionesDeOrden:
    def test_cancelar_antes_de_cobrar(self):
        assert can_transition(OrderState.CREATED, OrderState.CANCELLED)

    def test_no_se_cancela_una_orden_pagada(self):
        """Una orden pagada no se 'cancela': se reembolsa."""
        assert not can_transition(OrderState.PAID, OrderState.CANCELLED)

    def test_fallo_dispara_reembolso(self):
        assert can_transition(OrderState.FAILED, OrderState.REFUND_PENDING)

    def test_expiracion_solo_desde_pendiente_de_pago(self):
        assert can_transition(OrderState.PAYMENT_PENDING, OrderState.EXPIRED)
        assert not can_transition(OrderState.PAID, OrderState.EXPIRED)

    def test_revision_puede_resolverse_en_ambos_sentidos(self):
        """UNDER_REVIEW es un limbo del que se sale con informacion real,
        hacia exito o hacia fallo."""
        destinos = ORDER_TRANSITIONS[OrderState.UNDER_REVIEW]
        assert OrderState.SUCCESS in destinos
        assert OrderState.FAILED in destinos
        assert OrderState.PAID in destinos

    @pytest.mark.parametrize(
        "estado_final",
        [
            OrderState.SUCCESS,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REFUNDED,
        ],
    )
    def test_los_estados_finales_no_admiten_transicion(self, estado_final):
        assert ORDER_TRANSITIONS[estado_final] == frozenset()
        assert estado_final in TERMINAL_ORDER_STATES
        with pytest.raises(IllegalTransition):
            assert_transition(estado_final, OrderState.PROCESSING)


class TestIntegridadDelGrafo:
    def test_todos_los_estados_estan_declarados(self):
        """Si se agrega un estado nuevo y se olvida declarar sus transiciones,
        esta prueba lo detecta antes de que llegue a produccion."""
        assert set(ORDER_TRANSITIONS) == set(OrderState)

    def test_todos_los_destinos_son_estados_validos(self):
        for origen, destinos in ORDER_TRANSITIONS.items():
            for destino in destinos:
                assert destino in OrderState, f"{origen} -> {destino} no es un estado"

    def test_ningun_estado_transiciona_a_si_mismo(self):
        for origen, destinos in ORDER_TRANSITIONS.items():
            assert origen not in destinos, f"{origen} transiciona a si mismo"

    def test_estados_con_dinero_retenido(self):
        """Los estados en los que ya cobramos deben poder llegar a reembolso,
        directamente o a traves de otro estado."""
        assert OrderState.PAID in MONEY_HELD_STATES
        assert OrderState.SUCCESS in MONEY_HELD_STATES
        assert OrderState.CREATED not in MONEY_HELD_STATES
        assert OrderState.CANCELLED not in MONEY_HELD_STATES


class TestFulfillment:
    def test_no_se_envia_sin_pago_confirmado(self):
        """El mismo principio en el lado de la entrega: de
        PENDING_PAYMENT no se salta a SENT."""
        assert not can_transition(
            FulfillmentState.PENDING_PAYMENT, FulfillmentState.SENT
        )
        assert not can_transition(
            FulfillmentState.PENDING_PAYMENT, FulfillmentState.SUCCEEDED
        )

    def test_el_camino_correcto(self):
        assert can_transition(FulfillmentState.PENDING_PAYMENT, FulfillmentState.QUEUED)
        assert can_transition(FulfillmentState.QUEUED, FulfillmentState.SENT)
        assert can_transition(FulfillmentState.SENT, FulfillmentState.SUCCEEDED)

    def test_enviado_puede_quedar_en_revision(self):
        """Un timeout tras enviar deja el resultado desconocido."""
        assert can_transition(FulfillmentState.SENT, FulfillmentState.UNDER_REVIEW)

    def test_una_entrega_exitosa_puede_revertirse(self):
        assert can_transition(FulfillmentState.SUCCEEDED, FulfillmentState.REVERSED)

    def test_todos_los_estados_declarados(self):
        assert set(FULFILLMENT_TRANSITIONS) == set(FulfillmentState)


class TestMensajesDeError:
    def test_el_error_dice_que_si_se_permitia(self):
        """Un error de transicion debe ser depurable sin abrir el codigo."""
        with pytest.raises(IllegalTransition) as exc:
            assert_transition(OrderState.CREATED, OrderState.SUCCESS)

        mensaje = str(exc.value)
        assert "CREATED" in mensaje
        assert "SUCCESS" in mensaje
        assert "PAYMENT_PENDING" in mensaje  # lista lo que si se permitia
