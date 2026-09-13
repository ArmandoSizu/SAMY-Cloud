"""Agregado ``TopupFulfillment``: la entrega de la recarga.

Es un agregado **separado** de la orden, que vive en el servicio de Pagos.
Motivo: el dinero y la entrega tienen ciclos de vida distintos y fallan de
forma independiente. Un cobro exitoso con una recarga fallida es un estado
real y frecuente (numero equivocado, operador caido), y el sistema tiene que
poder representarlo para poder reembolsar.

La entrega arranca en ``PENDING_PAYMENT`` y **solo** pasa a ``QUEUED`` cuando
llega el evento ``order.paid``. No existe ningun otro camino: la regla del
dinero se cumple tambien aqui, no solo en el servicio de pagos.
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


class TopupFulfillment(models.Model):
    """Una recarga solicitada por una tienda."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Contexto (sin FK: viven en otras bases de datos) --------------
    organization_id = models.UUIDField(db_index=True)
    store_id = models.UUIDField(db_index=True)
    requested_by_id = models.UUIDField(db_index=True)
    #: Orden en el servicio de Pagos. Es la que autoriza ejecutar.
    order_id = models.UUIDField(null=True, blank=True, db_index=True)

    # --- Que se recarga --------------------------------------------------
    product = models.ForeignKey(
        "catalog.TopupProduct", on_delete=models.PROTECT, related_name="fulfillments"
    )
    operator_name = models.CharField(max_length=100)
    product_label = models.CharField(max_length=160)

    #: Numero en formato internacional. Se usa para llamar al proveedor.
    phone_e164 = models.CharField(max_length=20)
    #: Numero enmascarado. Es el UNICO que se muestra en comprobantes, listas
    #: y logs. El completo solo se usa contra el proveedor.
    phone_masked = models.CharField(max_length=20)

    currency = models.CharField(max_length=3, default="MXN")
    amount_cents = models.BigIntegerField(validators=[MinValueValidator(1)])

    # --- Estado -----------------------------------------------------------
    state = models.CharField(
        max_length=24,
        choices=[(s.value, s.value) for s in FulfillmentState],
        default=FulfillmentState.PENDING_PAYMENT,
        db_index=True,
    )
    state_changed_at = models.DateTimeField(default=timezone.now)
    state_reason = models.CharField(max_length=255, blank=True, default="")

    # --- Proveedor ---------------------------------------------------------
    provider_slug = models.CharField(max_length=40, blank=True, default="")
    provider_mode = models.CharField(max_length=16, blank=True, default="")
    #: Identificador de la recarga en el proveedor. Clave de conciliacion.
    provider_reference = models.CharField(
        max_length=128, blank=True, default="", db_index=True
    )
    #: Folio que el operador entrega al cliente. Va en el comprobante: es lo
    #: que el cliente presenta si tiene que reclamar con su compania.
    operator_reference = models.CharField(max_length=128, blank=True, default="")
    #: Clave enviada al proveedor para que no recargue dos veces.
    idempotency_key = models.CharField(max_length=255, unique=True)

    attempts = models.PositiveSmallIntegerField(default=0)
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    raw_response = models.JSONField(default=dict, blank=True)

    #: Lo que esta recarga costo del lado del PROVEEDOR, y la evidencia con la
    #: que se calculo.
    #:
    #: Va separado de ``raw_response`` a proposito: ahi vive la respuesta del
    #: proveedor tal como llego, y mezclar conclusiones propias con datos
    #: ajenos en el mismo campo hace imposible saber, meses despues, cual de
    #: las dos cosas se esta leyendo.
    #:
    #: Guarda, cuando se conocen: ``comision_bps``, ``mecanismo``,
    #: ``costo_proveedor_cents``, ``comision_acreditada_cents``,
    #: ``extra_comision``, ``saldo_antes_cents`` y ``saldo_despues_cents``.
    #:
    #: Los dos saldos son la razon principal de que este campo exista: son la
    #: MEDICION que permite deducir el mecanismo de comision de un proveedor
    #: nuevo en vez de suponerlo. Si al recargar $100 la bolsa de plataforma
    #: baja $100, la comision se abona aparte; si baja $94.34, es un bono al
    #: fondear; si baja $94.00, es un descuento por transaccion. Los tres
    #: casos se distinguen con dos numeros, y solo si se guardaron.
    #:
    #: Lo que NO va aqui: el total cobrado al cliente ni el costo de la
    #: pasarela. Esos dependen del metodo de pago y viven en la orden, en el
    #: servicio de Pagos. Copiarlos aqui crearia una segunda version de la
    #: verdad sobre cuanto pago un cliente.
    economia = models.JSONField(default=dict, blank=True)

    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Recarga"
        verbose_name_plural = "Recargas"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store_id", "-created_at"]),
            models.Index(fields=["state", "created_at"]),
            models.Index(
                fields=["created_at"],
                name="topup_needs_recon_idx",
                condition=models.Q(state="UNDER_REVIEW"),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(state__in=[s.value for s in FulfillmentState]),
                name="fulfillment_state_is_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0),
                name="fulfillment_amount_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.operator_name} {self.phone_masked} ({self.state})"

    @property
    def amount(self) -> Money:
        return Money(self.amount_cents, self.currency)

    @property
    def state_enum(self) -> FulfillmentState:
        return FulfillmentState(self.state)

    @transaction.atomic
    def transition(
        self,
        target: FulfillmentState,
        *,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "TopupFulfillment":
        """Cambia de estado validando la maquina de estados.

        ``select_for_update`` evita que el webhook del proveedor y el
        reintento de la tarea apliquen dos transiciones sobre el mismo estado.
        """
        current = TopupFulfillment.objects.select_for_update().get(pk=self.pk)
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
            update_fields=[
                "state",
                "state_changed_at",
                "state_reason",
                "sent_at",
                "completed_at",
                "updated_at",
            ]
        )

        log.info(
            "fulfillment_state_changed",
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
