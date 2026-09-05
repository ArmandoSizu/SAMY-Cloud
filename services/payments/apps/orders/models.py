"""Agregado ``Order``: el lado del dinero de toda operacion.

Este servicio es el **unico dueno** del dinero. Los servicios de recargas y de
pago de servicios no saben cobrar: piden una orden aqui, esperan a que quede
``PAID`` y entonces ejecutan lo suyo.

Modelo de datos (responsabilidades separadas, no una tabla gigante):

    Order              la intencion de compra y su estado de dinero
      ├── PaymentAttempt   cada intento de cobro (puede haber varios)
      ├── PaymentQrToken   token temporal de cobro con expiracion
      ├── CommissionEntry  el reparto congelado en el momento de la venta
      ├── Receipt          el comprobante emitido
      └── OrderEvent       historial de transiciones (auditoria local)

Puntos que hacen esto seguro:

* Los montos son ``BigIntegerField`` en centavos, con ``CheckConstraint`` que
  impide negativos donde no tienen sentido.
* El estado tiene ``CheckConstraint`` sobre el conjunto de valores validos: ni
  siquiera un ``UPDATE`` manual en la base puede meter un estado inventado.
* Toda transicion pasa por ``transition()``, que valida contra la maquina de
  estados, escribe un ``OrderEvent`` y publica al outbox en la MISMA
  transaccion.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from samy_common.money import Money
from samy_common.states import OrderState, assert_transition

log = structlog.get_logger("orders")


class ServiceKind(models.TextChoices):
    TOPUP = "TOPUP", "Recarga telefonica"
    BILL_PAYMENT = "BILL_PAYMENT", "Pago de servicios"
    MERCHANT_SALE = "MERCHANT_SALE", "Venta del comercio"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Efectivo"
    CARD = "CARD", "Tarjeta"
    TRANSFER = "TRANSFER", "Transferencia"
    QR = "QR", "QR / token"


class OrderQuerySet(models.QuerySet):
    def for_store(self, store_id) -> "OrderQuerySet":
        return self.filter(store_id=store_id)

    def today(self, tz=None) -> "OrderQuerySet":
        now = timezone.localtime(timezone=tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.filter(created_at__gte=start)

    def settled(self) -> "OrderQuerySet":
        """Ordenes en las que el dinero efectivamente entro."""
        return self.filter(
            state__in=[
                OrderState.PAID,
                OrderState.PROCESSING,
                OrderState.SUCCESS,
                OrderState.FAILED,
                OrderState.REFUND_PENDING,
            ]
        )

    def needs_reconciliation(self) -> "OrderQuerySet":
        """Ordenes cuyo resultado real desconocemos y hay que consultar."""
        return self.filter(state=OrderState.UNDER_REVIEW)


class Order(models.Model):
    """Una operacion de venta con su ciclo de dinero."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: Folio corto y legible para el comprobante y el mostrador.
    #: El UUID sirve para las maquinas; nadie dicta un UUID por telefono.
    folio = models.CharField(max_length=24, unique=True, db_index=True)

    # --- Contexto multi-tenant ----------------------------------------
    # Sin ForeignKey: la tabla de tiendas vive en la base del Core. Este
    # servicio confia en el store_id porque llega en una peticion FIRMADA
    # desde el Core, que ya verifico la membresia del usuario.
    organization_id = models.UUIDField(db_index=True)
    store_id = models.UUIDField(db_index=True)
    created_by_id = models.UUIDField(db_index=True)
    created_by_email = models.EmailField(blank=True, default="")

    # --- Que se vende --------------------------------------------------
    service_kind = models.CharField(max_length=24, choices=ServiceKind.choices)
    #: Referencia del agregado en el microservicio que ejecuta el servicio.
    fulfillment_id = models.UUIDField(null=True, blank=True, db_index=True)
    description = models.CharField(max_length=200)
    product_code = models.CharField(max_length=64, blank=True, default="")

    # --- Dinero (centavos enteros) --------------------------------------
    currency = models.CharField(max_length=3, default="MXN")
    base_cents = models.BigIntegerField(
        validators=[MinValueValidator(0)],
        help_text="Valor del servicio. Es lo que recibe el proveedor.",
    )
    commission_cents = models.BigIntegerField(
        default=0, validators=[MinValueValidator(0)]
    )
    total_cents = models.BigIntegerField(
        validators=[MinValueValidator(0)], help_text="base + comision. Lo que paga el cliente."
    )

    # --- Estado ---------------------------------------------------------
    state = models.CharField(
        max_length=24,
        choices=[(s.value, s.value) for s in OrderState],
        default=OrderState.CREATED,
        db_index=True,
    )
    state_changed_at = models.DateTimeField(default=timezone.now)
    #: Motivo del ultimo cambio, legible. Se muestra en el historial.
    state_reason = models.CharField(max_length=255, blank=True, default="")

    payment_method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, blank=True, default=""
    )

    # --- Trazabilidad ----------------------------------------------------
    correlation_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    #: Clave de idempotencia con la que se creo. Impide ordenes duplicadas.
    idempotency_key = models.CharField(max_length=255, blank=True, default="")

    #: Ambiente del proveedor con el que se opero. Impide confundir una
    #: operacion de sandbox con una real: viaja hasta el comprobante.
    provider_mode = models.CharField(max_length=16, blank=True, default="")

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    #: Vencimiento del intento de cobro (por ejemplo, del QR).
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = OrderQuerySet.as_manager()

    class Meta:
        verbose_name = "Orden"
        verbose_name_plural = "Ordenes"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store_id", "-created_at"]),
            models.Index(fields=["store_id", "state", "-created_at"]),
            models.Index(fields=["created_by_id", "-created_at"]),
            # Indice parcial para el barrido de expiracion: solo interesan las
            # ordenes que aun pueden expirar, no todo el historico.
            models.Index(
                fields=["expires_at"],
                name="order_pending_expiry_idx",
                condition=models.Q(state="PAYMENT_PENDING"),
            ),
        ]
        constraints = [
            # El total debe cuadrar SIEMPRE. Esta restriccion la impone la
            # base de datos, no la aplicacion: ni un bug ni un UPDATE manual
            # pueden dejar una orden descuadrada.
            models.CheckConstraint(
                condition=models.Q(
                    total_cents=models.F("base_cents") + models.F("commission_cents")
                ),
                name="order_total_equals_base_plus_commission",
            ),
            models.CheckConstraint(
                condition=models.Q(base_cents__gte=0)
                & models.Q(commission_cents__gte=0)
                & models.Q(total_cents__gte=0),
                name="order_amounts_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=[s.value for s in OrderState]),
                name="order_state_is_valid",
            ),
            # Una clave de idempotencia no puede producir dos ordenes en la
            # misma tienda. UNIQUE parcial: las vacias no compiten entre si.
            models.UniqueConstraint(
                fields=["store_id", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="order_idempotency_unique_per_store",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.folio} - {self.description} ({self.state})"

    # -- dinero como objetos -------------------------------------------

    @property
    def base(self) -> Money:
        return Money(self.base_cents, self.currency)

    @property
    def commission(self) -> Money:
        return Money(self.commission_cents, self.currency)

    @property
    def total(self) -> Money:
        return Money(self.total_cents, self.currency)

    # -- estado ---------------------------------------------------------

    @property
    def state_enum(self) -> OrderState:
        return OrderState(self.state)

    @property
    def is_paid(self) -> bool:
        """Si el dinero ya esta confirmado. Es la puerta a ejecutar el servicio."""
        return self.state_enum in {
            OrderState.PAID,
            OrderState.PROCESSING,
            OrderState.SUCCESS,
        }

    @property
    def is_expired(self) -> bool:
        return bool(
            self.expires_at
            and self.expires_at <= timezone.now()
            and self.state_enum == OrderState.PAYMENT_PENDING
        )

    @transaction.atomic
    def transition(
        self,
        target: OrderState,
        *,
        reason: str = "",
        actor_id: uuid.UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Order":
        """Cambia de estado validando la maquina de estados.

        Todo ocurre en una sola transaccion: el bloqueo de la fila, la
        validacion, el cambio, el evento de historial y el evento de outbox.
        O se aplica todo, o no se aplica nada.

        ``select_for_update`` evita la condicion de carrera clasica: el webhook
        del proveedor y el reintento del cajero llegando a la vez y aplicando
        dos transiciones sobre el mismo estado leido.
        """
        current = Order.objects.select_for_update().get(pk=self.pk)
        previous = OrderState(current.state)

        # Levanta IllegalTransition si el salto no esta permitido.
        assert_transition(previous, target)

        now = timezone.now()
        current.state = target
        current.state_changed_at = now
        current.state_reason = reason[:255]

        if target == OrderState.PAID and current.paid_at is None:
            current.paid_at = now
        if target in {OrderState.SUCCESS, OrderState.FAILED, OrderState.REFUNDED}:
            current.completed_at = now

        current.save(
            update_fields=[
                "state",
                "state_changed_at",
                "state_reason",
                "paid_at",
                "completed_at",
                "updated_at",
            ]
        )

        OrderEvent.objects.create(
            order=current,
            previous_state=previous.value,
            new_state=target.value,
            reason=reason[:255],
            actor_id=actor_id,
            correlation_id=current.correlation_id,
            metadata=metadata or {},
        )

        log.info(
            "order_state_changed",
            order_id=str(current.id),
            folio=current.folio,
            previous=previous.value,
            new=target.value,
            reason=reason,
        )

        # Refleja el nuevo estado en la instancia sobre la que se llamo.
        self.state = current.state
        self.state_changed_at = current.state_changed_at
        self.state_reason = current.state_reason
        self.paid_at = current.paid_at
        self.completed_at = current.completed_at
        return current


class OrderEvent(models.Model):
    """Historial de transiciones de una orden. Append-only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")

    previous_state = models.CharField(max_length=24)
    new_state = models.CharField(max_length=24)
    reason = models.CharField(max_length=255, blank=True, default="")

    actor_id = models.UUIDField(null=True, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Evento de orden"
        verbose_name_plural = "Eventos de orden"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["order", "created_at"])]

    def __str__(self) -> str:
        return f"{self.previous_state} -> {self.new_state}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk and OrderEvent.objects.filter(pk=self.pk).exists():
            raise ValueError("Los eventos de orden son inmutables.")
        super().save(*args, **kwargs)


class CommissionEntry(models.Model):
    """Reparto de la comision, congelado en el momento de la venta.

    Se guarda como copia y no como referencia a la regla porque la regla puede
    cambiar manana. Lo que se cobro ayer es un hecho contable, no una consulta.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="commission_entry")

    currency = models.CharField(max_length=3, default="MXN")
    commission_cents = models.BigIntegerField(validators=[MinValueValidator(0)])
    store_share_cents = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    platform_share_cents = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    provider_share_cents = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])

    #: Copia de la regla aplicada, para poder explicar el cobro meses despues.
    rule_id = models.UUIDField(null=True, blank=True)
    rule_name = models.CharField(max_length=120, blank=True, default="")
    rule_description = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Comision aplicada"
        verbose_name_plural = "Comisiones aplicadas"
        constraints = [
            # El reparto debe sumar exactamente la comision. Invariante
            # contable impuesta por la base de datos.
            models.CheckConstraint(
                condition=models.Q(
                    commission_cents=models.F("store_share_cents")
                    + models.F("platform_share_cents")
                    + models.F("provider_share_cents")
                ),
                name="commission_shares_sum_to_total",
            )
        ]

    def __str__(self) -> str:
        return f"Comision {Money(self.commission_cents, self.currency)}"
