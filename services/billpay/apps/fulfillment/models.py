"""Agregado ``BillPaymentFulfillment``: el pago de un recibo.

Igual que en recargas, es un agregado separado de la orden: el dinero y la
entrega fallan de forma independiente. Un cobro exitoso con un pago de recibo
fallido es un estado real, y el sistema tiene que poder representarlo para
poder reembolsar.

La referencia se guarda completa (se necesita para pagarle al agregador) y
enmascarada (es la unica que se muestra en comprobantes, listas y logs).
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from samy_common.money import Money
from samy_common.states import FulfillmentState, assert_transition

log = structlog.get_logger("fulfillment")


class BillPaymentFulfillment(models.Model):
    """Un pago de recibo solicitado por una tienda."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization_id = models.UUIDField(db_index=True)
    store_id = models.UUIDField(db_index=True)
    requested_by_id = models.UUIDField(db_index=True)
    order_id = models.UUIDField(null=True, blank=True, db_index=True)

    biller = models.ForeignKey(
        "billers.Biller", on_delete=models.PROTECT, related_name="payments"
    )
    biller_name = models.CharField(max_length=120)

    #: Referencia completa. Solo se usa contra el agregador.
    reference = models.CharField(max_length=64)
    #: Referencia enmascarada. Es la que se muestra en cualquier salida.
    reference_masked = models.CharField(max_length=64)

    currency = models.CharField(max_length=3, default="MXN")
    amount_cents = models.BigIntegerField(validators=[MinValueValidator(1)])

    #: Datos de la consulta de adeudo, congelados en el momento de consultar.
    customer_name = models.CharField(max_length=160, blank=True, default="")
    period = models.CharField(max_length=60, blank=True, default="")
    due_date = models.DateField(null=True, blank=True)

    state = models.CharField(
        max_length=24,
        choices=[(s.value, s.value) for s in FulfillmentState],
        default=FulfillmentState.PENDING_PAYMENT,
        db_index=True,
    )
    state_changed_at = models.DateTimeField(default=timezone.now)
    state_reason = models.CharField(max_length=255, blank=True, default="")

    provider_slug = models.CharField(max_length=40, blank=True, default="")
    provider_mode = models.CharField(max_length=16, blank=True, default="")
    provider_reference = models.CharField(
        max_length=128, blank=True, default="", db_index=True
    )
    #: Folio que entrega el biller. Es lo que el cliente presenta si reclama.
    biller_reference = models.CharField(max_length=128, blank=True, default="")
    idempotency_key = models.CharField(max_length=255, unique=True)

    attempts = models.PositiveSmallIntegerField(default=0)
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    raw_response = models.JSONField(default=dict, blank=True)

    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Pago de servicio"
        verbose_name_plural = "Pagos de servicios"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store_id", "-created_at"]),
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["biller", "reference"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(state__in=[s.value for s in FulfillmentState]),
                name="billpay_state_is_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0),
                name="billpay_amount_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.biller_name} {self.reference_masked} ({self.state})"

    @property
    def amount(self) -> Money:
        return Money(self.amount_cents, self.currency)

    @property
    def state_enum(self) -> FulfillmentState:
        return FulfillmentState(self.state)

    @transaction.atomic
    def transition(
        self, target: FulfillmentState, *, reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "BillPaymentFulfillment":
        current = BillPaymentFulfillment.objects.select_for_update().get(pk=self.pk)
        previous = FulfillmentState(current.state)

        assert_transition(previous, target)

        now = timezone.now()
        current.state = target
        current.state_changed_at = now
        current.state_reason = reason[:255]

        if target == FulfillmentState.SENT and current.sent_at is None:
            current.sent_at = now
        if target in {FulfillmentState.SUCCEEDED, FulfillmentState.FAILED}:
            current.completed_at = now

        current.save(
            update_fields=["state", "state_changed_at", "state_reason",
                           "sent_at", "completed_at", "updated_at"]
        )

        log.info(
            "billpay_state_changed",
            fulfillment_id=str(current.id),
            order_id=str(current.order_id) if current.order_id else None,
            previous=previous.value,
            new=target.value,
            reason=reason,
        )

        self.state = current.state
        self.state_changed_at = current.state_changed_at
        self.sent_at = current.sent_at
        self.completed_at = current.completed_at
        return current


class BillInquiryLog(models.Model):
    """Registro de cada consulta de adeudo.

    Sirve para dos cosas: auditar quien consulto que referencia (es un dato de
    un cliente), y detectar consultas repetidas que podrian indicar un intento
    de enumerar referencias ajenas.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    biller = models.ForeignKey(
        "billers.Biller", on_delete=models.CASCADE, related_name="inquiries"
    )
    store_id = models.UUIDField(db_index=True)
    requested_by_id = models.UUIDField(null=True, blank=True)

    reference_masked = models.CharField(max_length=64)
    found = models.BooleanField()
    amount_cents = models.BigIntegerField(null=True, blank=True)
    source = models.CharField(
        max_length=16, default="manual", help_text="'scan' o 'manual'."
    )

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Consulta de adeudo"
        verbose_name_plural = "Consultas de adeudo"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["store_id", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.biller_id} {self.reference_masked} ({'ok' if self.found else 'no encontrado'})"
