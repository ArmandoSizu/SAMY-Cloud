"""Consumo de los resultados de entrega que publican los microservicios ejecutores.

Por que existe este archivo
---------------------------
La regla del dinero define la cadena completa::

    PAYMENT_PENDING -> PAID -> PROCESSING -> SUCCESS

Pagos sabia llevar la orden hasta ``PAID``, y Recargas sabia ejecutar y
publicar ``fulfillment.result`` en su stream. Pero **nadie leia ese stream**:
``record_fulfillment_result`` existia, estaba probado y no lo invocaba nada en
produccion. El efecto real, observado con una recarga de sandbox entregada de
verdad por el proveedor: la recarga quedaba ``SUCCEEDED`` y la orden se
quedaba en ``PAID`` para siempre, con el comprobante mostrando
"PAGADA - EN PROCESO" a un cliente que ya habia recibido su saldo.

Este consumidor cierra esa cadena.

Decisiones que importan
-----------------------
* **Idempotente.** La entrega del bus es "al menos una vez". Un evento
  repetido no puede volver a cerrar una orden ni disparar un segundo
  reembolso, asi que las ordenes ya en estado final se ignoran.
* **No se confia en el evento como prueba.** El evento dice "esta entrega
  termino"; el estado que se aplica sale de la orden real en esta base de
  datos, y las transiciones las valida la maquina de estados.
* **Un evento que falla NO se confirma.** Queda pendiente y se reentrega. Un
  ``fulfillment.result`` perdido significa un cliente que pago, recibio (o no)
  su servicio, y cuya orden nunca se cierra ni se reembolsa.
"""

from __future__ import annotations

import redis
import structlog
from celery import shared_task
from django.conf import settings

from apps.orders import services
from apps.orders.models import Order
from samy_common.events.bus import Event, RedisStreamBus
from samy_common.states import OrderState

log = structlog.get_logger("orders.consumers")

BATCH_SIZE = 20

#: Estados desde los que ya no hay nada que cerrar. Un evento repetido que
#: llegue con la orden aqui se confirma y se descarta sin tocar nada.
ESTADOS_FINALES = frozenset(
    {
        OrderState.SUCCESS,
        OrderState.FAILED,
        OrderState.REFUND_PENDING,
        OrderState.REFUNDED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    }
)


@shared_task(name="apps.orders.consumers.consume_fulfillment_events")
def consume_fulfillment_events() -> dict[str, int]:
    """Lee los resultados de entrega y cierra las ordenes correspondientes."""
    client = redis.Redis.from_url(settings.EVENT_STREAM_URL)
    procesados = 0
    aplicados = 0

    for stream_name in settings.FULFILLMENT_STREAM_NAMES:
        bus = RedisStreamBus(client, stream_name)
        bus.ensure_group(settings.EVENT_CONSUMER_GROUP)

        mensajes = client.xreadgroup(
            groupname=settings.EVENT_CONSUMER_GROUP,
            consumername=f"{settings.SERVICE_NAME}-worker",
            streams={stream_name: ">"},
            count=BATCH_SIZE,
            block=1000,
        )

        for _stream, entradas in mensajes or []:
            for message_id, raw in entradas:
                try:
                    event = Event.from_wire(raw)
                except Exception as exc:  # noqa: BLE001
                    # Se confirma para no atascar la cola, pero queda como
                    # error: hay una orden que quiza nunca se cierre sola.
                    # La barrida de conciliacion es la red de seguridad.
                    log.error(
                        "fulfillment_event_unparseable",
                        stream=stream_name,
                        message_id=str(message_id),
                        error=str(exc),
                    )
                    client.xack(stream_name, settings.EVENT_CONSUMER_GROUP, message_id)
                    continue

                try:
                    if event.event_type == "fulfillment.result":
                        if _aplicar_resultado(event):
                            aplicados += 1
                    procesados += 1
                except Exception as exc:  # noqa: BLE001
                    # Sin XACK: se reentrega. Perder esto deja una orden
                    # cobrada sin cerrar ni reembolsar.
                    log.error(
                        "fulfillment_event_handling_failed",
                        event_type=event.event_type,
                        aggregate_id=event.aggregate_id,
                        error=str(exc),
                        exc_info=True,
                    )
                    continue

                client.xack(stream_name, settings.EVENT_CONSUMER_GROUP, message_id)

    if procesados:
        log.info(
            "fulfillment_events_consumed", procesados=procesados, aplicados=aplicados
        )
    return {"procesados": procesados, "aplicados": aplicados}


def _aplicar_resultado(event: Event) -> bool:
    """Cierra la orden segun lo que reporto el servicio ejecutor."""
    payload = event.payload
    order_id = payload.get("order_id")
    if not order_id:
        log.error("fulfillment_result_sin_order_id", event_id=event.event_id)
        return False

    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        log.error("fulfillment_result_orden_inexistente", order_id=order_id)
        return False

    if order.state_enum in ESTADOS_FINALES:
        log.info(
            "fulfillment_result_ya_aplicado",
            order_id=order_id,
            state=order.state,
        )
        return False

    # Respuesta indeterminada del proveedor: no se cierra ni a favor ni en
    # contra. La conciliacion de Recargas consultara el estado real y volvera
    # a publicar cuando lo sepa. Cerrarla ahora seria inventar el desenlace.
    if payload.get("pending_review"):
        if order.state_enum == OrderState.PAID:
            order = services.mark_processing(
                order=order, reason="Entrega enviada, resultado por confirmar."
            )
        log.warning(
            "fulfillment_result_indeterminado",
            order_id=order_id,
            folio=order.folio,
            state=order.state,
        )
        return False

    # PAID -> PROCESSING es obligatorio: no existe arista PAID -> SUCCESS.
    # Esa ausencia es deliberada, y respetarla deja el rastro completo de por
    # donde paso el dinero.
    if order.state_enum == OrderState.PAID:
        order = services.mark_processing(
            order=order, reason="Servicio en ejecucion en el proveedor."
        )

    if order.state_enum != OrderState.PROCESSING:
        log.error(
            "fulfillment_result_estado_inesperado",
            order_id=order_id,
            state=order.state,
        )
        return False

    exito = bool(payload.get("succeeded"))
    services.record_fulfillment_result(
        order=order,
        succeeded=exito,
        provider_reference=str(payload.get("provider_reference") or "")[:128],
        reason=str(payload.get("failure_reason") or "")[:255],
    )
    log.info(
        "fulfillment_result_aplicado",
        order_id=order_id,
        folio=order.folio,
        succeeded=exito,
    )
    return True
