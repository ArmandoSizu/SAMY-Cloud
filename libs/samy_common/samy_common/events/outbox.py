"""Patron Transactional Outbox para eventos entre microservicios.

El problema: cuando el servicio de Pagos confirma un cobro tiene que (a)
guardar la orden como PAID y (b) avisar al servicio de Recargas. Si se hace
"guardar en BD y luego publicar en Redis", existe una ventana en la que la BD
ya se confirmo y el proceso muere antes de publicar: el cliente pago y nunca
recibe su recarga. Publicar primero tiene el defecto simetrico.

La solucion estandar es el **outbox transaccional**: el evento se escribe en
una tabla de la MISMA base de datos y dentro de la MISMA transaccion que el
cambio de estado. O se guardan ambos, o ninguno. Un proceso aparte (Celery
beat) lee la tabla y publica al bus, marcando cada evento como publicado.

Esto da entrega **al menos una vez**, por eso todo consumidor debe ser
idempotente (ver ``samy_common.idempotency``).
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class OutboxStatus(models.TextChoices):
    PENDING = "PENDING", "Pendiente"
    PUBLISHED = "PUBLISHED", "Publicado"
    FAILED = "FAILED", "Fallido"


class AbstractOutboxEvent(models.Model):
    """Evento pendiente de publicar. Cada servicio crea su tabla concreta."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: Nombre del evento, ej. ``order.paid``, ``fulfillment.succeeded``.
    event_type = models.CharField(max_length=100, db_index=True)
    #: Tipo y id del agregado que origino el evento.
    aggregate_type = models.CharField(max_length=64)
    aggregate_id = models.UUIDField(db_index=True)
    #: Cuerpo del evento. Nunca contiene datos sensibles de tarjeta.
    payload = models.JSONField()
    #: Viaja con el evento para poder encadenar la traza distribuida.
    correlation_id = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=OutboxStatus.choices, default=OutboxStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    #: Proximo intento; permite backoff exponencial sin bloquear la cola.
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        abstract = True
        indexes = [
            # Indice parcial: solo interesan los pendientes, que son pocos
            # frente al historico de publicados.
            models.Index(
                fields=["next_attempt_at"],
                name="%(app_label)s_%(class)s_due_idx",
                condition=models.Q(status="PENDING"),
            ),
        ]
        ordering = ["created_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.event_type}({self.aggregate_id}) [{self.status}]"

    def mark_published(self) -> None:
        self.status = OutboxStatus.PUBLISHED
        self.published_at = timezone.now()
        self.save(update_fields=["status", "published_at"])

    def mark_failed(self, error: str, *, backoff_seconds: int) -> None:
        self.attempts += 1
        self.last_error = error[:2000]
        self.next_attempt_at = timezone.now() + timezone.timedelta(
            seconds=backoff_seconds
        )
        # Se mantiene PENDING para que el drenador lo reintente; solo tras
        # agotar los intentos maximos lo marca FAILED quien invoca.
        self.save(
            update_fields=["attempts", "last_error", "next_attempt_at", "status"]
        )
