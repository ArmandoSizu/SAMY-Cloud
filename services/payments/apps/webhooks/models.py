"""Registro durable de los webhooks recibidos.

Por que no basta con deduplicar en cache:

La deduplicacion por Redis funciona y es la primera barrera, pero Redis es
memoria: se limpia al reiniciarlo, al llenarse, o al cambiar de instancia. Un
proveedor que reenvie un evento de hace dos dias justo despues de un reinicio
lo encontraria "nuevo".

En este sistema eso no llega a duplicar dinero, porque ``confirm_payment`` es
idempotente y una orden ya PAID ignora confirmaciones repetidas. Pero
"no duplica gracias a otra capa" es una defensa mas fragil que "no se procesa
dos veces, punto", y aqui hablamos de webhooks que disparan recargas.

Ademas resuelve algo que la cache no puede: **saber que llego**. Cuando un
cobro no aparece, la primera pregunta es si el proveedor aviso; sin esta tabla
la respuesta es "no lo sabemos". Con ella se ve el evento, su firma, cuando
llego y que se hizo con el.

Se guarda el cuerpo saneado, nunca datos de tarjeta: el adaptador ya los
elimina antes de que la respuesta salga de el.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class WebhookProcessingResult(models.TextChoices):
    """Que se hizo con el evento. Es lo que se consulta al investigar."""

    APPLIED = "APPLIED", "Aplicado"
    DUPLICATE = "DUPLICATE", "Duplicado ignorado"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND", "Sin orden asociada"
    REJECTED = "REJECTED", "Rechazado"
    IGNORED = "IGNORED", "Sin efecto (estado no terminal)"


class ReceivedWebhook(models.Model):
    """Un evento entrante, ya verificado criptograficamente.

    Solo se guardan eventos con FIRMA VALIDA. Los rechazados quedan en el log
    de seguridad, no aqui: esta tabla es el registro de lo que el proveedor
    nos dijo de verdad, y mezclarlo con intentos de suplantacion la volveria
    inutil como evidencia.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    provider_slug = models.CharField(max_length=40, db_index=True)
    #: Identificador del evento EN EL PROVEEDOR. Es la clave de deduplicacion.
    event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=80, blank=True, default="")

    #: Referencia del cargo en el proveedor, para cruzarlo con el intento.
    provider_reference = models.CharField(max_length=128, blank=True, default="", db_index=True)

    outcome = models.CharField(max_length=16, blank=True, default="")
    amount_cents = models.BigIntegerField(null=True, blank=True)

    result = models.CharField(
        max_length=24,
        choices=WebhookProcessingResult.choices,
        default=WebhookProcessingResult.IGNORED,
    )
    detail = models.CharField(max_length=300, blank=True, default="")

    #: Cuerpo saneado. Sin numero de tarjeta ni CVV: el adaptador los elimina.
    payload = models.JSONField(default=dict, blank=True)

    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Webhook recibido"
        verbose_name_plural = "Webhooks recibidos"
        ordering = ["-received_at"]
        constraints = [
            # La deduplicacion REAL vive aqui, en la base, no en una
            # comprobacion previa: dos peticiones simultaneas del mismo evento
            # pasarian las dos por un "si existe", pero solo una puede ganar
            # una restriccion unica.
            models.UniqueConstraint(
                fields=["provider_slug", "event_id"],
                name="webhook_unique_per_provider_event",
            )
        ]
        indexes = [
            models.Index(fields=["provider_slug", "-received_at"]),
            models.Index(fields=["event_type", "-received_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.provider_slug}:{self.event_type} {self.event_id}"
