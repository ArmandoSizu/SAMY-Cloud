"""Idempotencia a nivel de base de datos.

El requisito es explicito: un webhook entregado dos veces o un reintento del
cajero NO deben producir dos recargas ni dos cobros.

La garantia no puede depender de una comprobacion en Python del tipo
"consulta si existe, y si no, crea": entre la consulta y la creacion caben dos
peticiones concurrentes. La unica garantia real es una **restriccion UNIQUE en
la base de datos** dentro de una transaccion.

Flujo:

1. Se intenta ``INSERT`` del registro de idempotencia con estado IN_PROGRESS.
2. Si el INSERT viola el UNIQUE -> otra peticion identica ya llego.
   - Si la primera termino, se devuelve su respuesta guardada (mismo resultado).
   - Si sigue en curso, se responde 409 y el cliente reintenta despues.
3. Al terminar, se guarda la respuesta y se marca COMPLETED.

Ademas se guarda un hash del cuerpo: si llega la misma clave con un cuerpo
distinto, es un error del cliente y se rechaza con 422, en vez de devolver
silenciosamente la respuesta de otra operacion.
"""

from __future__ import annotations

import hashlib
import uuid

from django.db import models
from django.utils import timezone


def hash_payload(body: bytes) -> str:
    """SHA-256 del cuerpo de la peticion, para detectar reuso incorrecto de clave."""
    return hashlib.sha256(body).hexdigest()


class IdempotencyStatus(models.TextChoices):
    IN_PROGRESS = "IN_PROGRESS", "En proceso"
    COMPLETED = "COMPLETED", "Completada"
    FAILED = "FAILED", "Fallida"


class AbstractIdempotencyRecord(models.Model):
    """Registro de idempotencia. Cada servicio crea su tabla concreta.

    Se mantiene por servicio (no compartida) porque cada microservicio es dueno
    de su propia base de datos: una tabla compartida seria acoplamiento de datos.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: Clave enviada por el cliente en la cabecera ``Idempotency-Key``.
    key = models.CharField(max_length=255)
    #: Ambito de la clave: endpoint + tenant. Evita colisiones entre tiendas.
    scope = models.CharField(max_length=255)
    #: Hash del cuerpo original, para detectar reuso de clave con otro payload.
    request_hash = models.CharField(max_length=64)

    status = models.CharField(
        max_length=20,
        choices=IdempotencyStatus.choices,
        default=IdempotencyStatus.IN_PROGRESS,
    )
    response_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    #: Momento a partir del cual el registro puede purgarse.
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "key"], name="%(app_label)s_%(class)s_scope_key_uniq"
            )
        ]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.scope}:{self.key} [{self.status}]"

    def mark_completed(self, status_code: int, body: dict | list | None) -> None:
        self.status = IdempotencyStatus.COMPLETED
        self.response_status_code = status_code
        self.response_body = body
        self.completed_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "response_status_code",
                "response_body",
                "completed_at",
            ]
        )

    def mark_failed(self, status_code: int, body: dict | list | None) -> None:
        """Un fallo tambien se registra: reintentar la misma clave tras un
        error de negocio debe devolver el mismo error, no ejecutar de nuevo."""
        self.status = IdempotencyStatus.FAILED
        self.response_status_code = status_code
        self.response_body = body
        self.completed_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "response_status_code",
                "response_body",
                "completed_at",
            ]
        )
