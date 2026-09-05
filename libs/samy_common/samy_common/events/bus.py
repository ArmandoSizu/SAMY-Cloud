"""Bus de eventos sobre Redis Streams.

Se eligio **Redis Streams** en vez de Kafka o RabbitMQ porque:

* Redis ya es parte del stack (cache, locks de idempotencia, Celery), asi que
  no agrega un componente de infraestructura nuevo que operar y pagar.
* Streams da grupos de consumidores, acuse explicito (``XACK``) y reentrega de
  mensajes no confirmados (``XAUTOCLAIM``), que es lo que necesitamos para
  entrega "al menos una vez".
* A la escala de este producto (miles de operaciones diarias, no millones por
  segundo) Kafka seria complejidad operativa sin beneficio.

La abstraccion ``EventBus`` deja la puerta abierta a cambiar a SNS/SQS,
Pub/Sub o Kafka sin tocar la logica de negocio, si el volumen lo justifica.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable, Iterator

import redis

from samy_common.observability.logging import get_logger

__all__ = ["Event", "EventBus", "RedisStreamBus", "InMemoryBus"]

log = get_logger("events")


@dataclass(frozen=True, slots=True)
class Event:
    """Evento de dominio publicado por un microservicio."""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    correlation_id: str = ""
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(dt_timezone.utc)
    )

    def to_wire(self) -> dict[str, str]:
        """Serializa a campos planos, que es lo que acepta Redis Streams."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "correlation_id": self.correlation_id,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": json.dumps(self.payload, separators=(",", ":")),
        }

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "Event":
        def _s(key: str) -> str:
            value = data.get(key, "")
            return value.decode() if isinstance(value, bytes) else str(value)

        return cls(
            event_id=_s("event_id"),
            event_type=_s("event_type"),
            aggregate_type=_s("aggregate_type"),
            aggregate_id=_s("aggregate_id"),
            correlation_id=_s("correlation_id"),
            occurred_at=datetime.fromisoformat(_s("occurred_at")),
            payload=json.loads(_s("payload") or "{}"),
        )


class EventBus(abc.ABC):
    """Contrato del bus. Permite sustituir la implementacion sin tocar dominio."""

    @abc.abstractmethod
    def publish(self, event: Event) -> str: ...

    @abc.abstractmethod
    def consume(
        self,
        *,
        group: str,
        consumer: str,
        handler: Callable[[Event], None],
        block_ms: int = 5000,
        batch_size: int = 10,
    ) -> Iterator[None]: ...


class RedisStreamBus(EventBus):
    """Implementacion sobre Redis Streams con grupos de consumidores."""

    def __init__(self, client: redis.Redis, stream: str, *, maxlen: int = 100_000):
        self.client = client
        self.stream = stream
        self.maxlen = maxlen

    def publish(self, event: Event) -> str:
        message_id = self.client.xadd(
            self.stream,
            event.to_wire(),
            maxlen=self.maxlen,
            approximate=True,
        )
        log.info(
            "event_published",
            event_type=event.event_type,
            aggregate_id=event.aggregate_id,
            stream=self.stream,
        )
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)

    def ensure_group(self, group: str) -> None:
        """Crea el grupo de consumidores si no existe (idempotente)."""
        try:
            self.client.xgroup_create(self.stream, group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def consume(
        self,
        *,
        group: str,
        consumer: str,
        handler: Callable[[Event], None],
        block_ms: int = 5000,
        batch_size: int = 10,
    ) -> Iterator[None]:
        """Consume indefinidamente. Solo hace ``XACK`` si el handler tuvo exito.

        Un mensaje cuyo handler falla queda en la lista de pendientes (PEL) y
        se reentrega, en vez de perderse silenciosamente.
        """
        self.ensure_group(group)
        while True:
            messages = self.client.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={self.stream: ">"},
                count=batch_size,
                block=block_ms,
            )
            if not messages:
                yield None
                continue

            for _stream, entries in messages:
                for message_id, raw in entries:
                    try:
                        event = Event.from_wire(raw)
                        handler(event)
                    except Exception as exc:  # noqa: BLE001
                        # No se hace XACK: el mensaje sigue pendiente y sera
                        # reentregado. Nunca se descarta por una excepcion.
                        log.error(
                            "event_handler_failed",
                            stream=self.stream,
                            message_id=str(message_id),
                            error=str(exc),
                            exc_info=True,
                        )
                    else:
                        self.client.xack(self.stream, group, message_id)
            yield None


class InMemoryBus(EventBus):
    """Bus en memoria para pruebas. No persiste ni reparte entre procesos."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish(self, event: Event) -> str:
        self.published.append(event)
        return event.event_id

    def consume(  # pragma: no cover - las pruebas leen ``published``
        self,
        *,
        group: str,
        consumer: str,
        handler: Callable[[Event], None],
        block_ms: int = 5000,
        batch_size: int = 10,
    ) -> Iterator[None]:
        for event in list(self.published):
            handler(event)
        yield None
