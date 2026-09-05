"""Bitacora de auditoria.

En un sistema que mueve dinero, "no se que paso" no es una respuesta
aceptable. Cada cambio de estado, cada intento de pago y cada acceso denegado
deja un registro inmutable con quien, cuando, desde donde y que cambio.

Decisiones de diseno:

* **Append-only**: no hay ``update`` ni ``delete``. Un registro que se puede
  editar no sirve como evidencia. ``save()`` bloquea la modificacion.
* **Datos minimos**: se guarda lo necesario para reconstruir la operacion, no
  todo lo que pasaba por ahi. Nunca PAN, CVV ni contrasenas.
* **Correlacion**: se guarda ``correlation_id`` para poder unir el rastro con
  los eventos de los otros tres microservicios.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditAction(models.TextChoices):
    # Sesion
    LOGIN_SUCCESS = "LOGIN_SUCCESS", "Inicio de sesion"
    LOGIN_FAILED = "LOGIN_FAILED", "Inicio de sesion fallido"
    LOGOUT = "LOGOUT", "Cierre de sesion"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED", "Solicitud de nueva contrasena"
    PASSWORD_CHANGED = "PASSWORD_CHANGED", "Contrasena modificada"
    PERMISSION_DENIED = "PERMISSION_DENIED", "Acceso denegado"
    STORE_SWITCHED = "STORE_SWITCHED", "Cambio de tienda"

    # Operaciones
    ORDER_CREATED = "ORDER_CREATED", "Orden creada"
    ORDER_STATE_CHANGED = "ORDER_STATE_CHANGED", "Cambio de estado de orden"
    PAYMENT_ATTEMPTED = "PAYMENT_ATTEMPTED", "Intento de pago"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED", "Pago confirmado"
    FULFILLMENT_REQUESTED = "FULFILLMENT_REQUESTED", "Servicio solicitado al proveedor"
    FULFILLMENT_RESULT = "FULFILLMENT_RESULT", "Respuesta del proveedor"
    REFUND_REQUESTED = "REFUND_REQUESTED", "Reembolso solicitado"

    # Configuracion
    CONFIG_CHANGED = "CONFIG_CHANGED", "Configuracion modificada"
    COMMISSION_RULE_CHANGED = "COMMISSION_RULE_CHANGED", "Regla de comision modificada"
    PROVIDER_CONFIG_CHANGED = "PROVIDER_CONFIG_CHANGED", "Proveedor reconfigurado"
    EMPLOYEE_ADDED = "EMPLOYEE_ADDED", "Empleado agregado"
    EMPLOYEE_REMOVED = "EMPLOYEE_REMOVED", "Empleado dado de baja"


class AuditEventQuerySet(models.QuerySet):
    def for_store(self, store_id) -> "AuditEventQuerySet":
        return self.filter(store_id=store_id)

    def for_correlation(self, correlation_id: str) -> "AuditEventQuerySet":
        """Todo el rastro de una operacion de negocio completa."""
        return self.filter(correlation_id=correlation_id).order_by("created_at")


class AuditEvent(models.Model):
    """Registro inmutable de un hecho relevante del sistema."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    action = models.CharField(max_length=48, choices=AuditAction.choices, db_index=True)

    # Quien. Se conserva el correo en texto porque el usuario puede borrarse y
    # el registro de auditoria debe seguir siendo legible.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    actor_email = models.EmailField(blank=True, default="")
    actor_role = models.CharField(max_length=32, blank=True, default="")

    # Donde
    store = models.ForeignKey(
        "tenancy.Store",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")
    session_key = models.CharField(max_length=64, blank=True, default="")

    # Sobre que
    object_type = models.CharField(max_length=64, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")

    # Cambio de estado (para transiciones de la maquina de estados)
    previous_state = models.CharField(max_length=32, blank=True, default="")
    new_state = models.CharField(max_length=32, blank=True, default="")

    # Contexto adicional. Nunca datos sensibles de tarjeta.
    metadata = models.JSONField(default=dict, blank=True)

    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        verbose_name = "Evento de auditoria"
        verbose_name_plural = "Eventos de auditoria"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store", "-created_at"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} por {self.actor_email or 'sistema'}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Append-only: una vez escrito, el registro no se modifica."""
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValueError(
                "Los eventos de auditoria son inmutables y no pueden modificarse."
            )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError(
            "Los eventos de auditoria no pueden borrarse. "
            "La retencion se gestiona por politica de archivado."
        )
