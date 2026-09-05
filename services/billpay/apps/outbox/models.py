"""Outbox transaccional del servicio de pago de servicios.

Ver ``samy_common.events.outbox`` para la explicacion del patron. Aqui solo se
concreta la tabla, porque cada microservicio tiene su propia base de datos y
por tanto su propio outbox.
"""

from __future__ import annotations

from samy_common.events.outbox import AbstractOutboxEvent, OutboxStatus

# Se reexporta para que las tareas importen ambos desde aqui y no tengan que
# conocer la ruta interna del paquete compartido.
__all__ = ["OutboxEvent", "OutboxStatus"]


class OutboxEvent(AbstractOutboxEvent):
    class Meta(AbstractOutboxEvent.Meta):
        db_table = "billpay_outbox_event"
        verbose_name = "Evento de outbox"
        verbose_name_plural = "Eventos de outbox"
