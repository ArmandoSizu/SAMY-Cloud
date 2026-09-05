"""Motor de comisiones configurable.

Requisito explicito del producto: **nada de porcentajes ni repartos
codificados**. La comision que se cobra al cliente y como se reparte entre la
tienda, SAMY Cloud y el costo del proveedor son parametros de negocio que
cambian por tienda, por servicio y con el tiempo.

Modelo conceptual, en dos partes separadas a proposito:

1. ``CommissionRule`` - **cuanto se le cobra al cliente**.
   Fija, porcentual o mixta. Con minimo y maximo opcionales.

2. ``CommissionSplit`` - **como se reparte** esa comision.
   Pesos enteros entre tienda, plataforma y reserva de costo del proveedor.

Separarlas importa: se puede cambiar el reparto interno sin tocar lo que paga
el cliente, y viceversa. Si estuvieran juntas, cada ajuste de margen obligaria
a re-publicar precios.

**Resolucion de reglas** (de mas especifica a mas general):

    tienda + producto  →  tienda + servicio  →  organizacion + servicio
      →  plan del servicio  →  regla por defecto de la plataforma

La primera que exista gana. Siempre hay una regla por defecto, de modo que
nunca ocurre "no se encontro regla" en medio de una venta.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from samy_common.money import Money


class ServiceKind(models.TextChoices):
    """Microservicio o linea de negocio a la que aplica la regla."""

    TOPUP = "TOPUP", "Recarga telefonica"
    BILL_PAYMENT = "BILL_PAYMENT", "Pago de servicios"
    #: Cobro generico del comercio (una venta propia de la tienda).
    MERCHANT_SALE = "MERCHANT_SALE", "Venta del comercio"


class CommissionType(models.TextChoices):
    FIXED = "FIXED", "Monto fijo"
    PERCENTAGE = "PERCENTAGE", "Porcentaje"
    MIXED = "MIXED", "Fijo + porcentaje"


class CommissionRule(models.Model):
    """Cuanto se le cobra de comision al cliente final.

    El ambito se define por los campos opcionales ``store_id``,
    ``organization_id`` y ``product_code``. Cuantos mas esten definidos, mas
    especifica es la regla y mayor su prioridad.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=120)
    service_kind = models.CharField(max_length=24, choices=ServiceKind.choices)

    # --- Ambito -------------------------------------------------------
    # Se guardan como UUID sueltos, sin ForeignKey: la tabla de tiendas vive
    # en la base de datos del Core y este servicio NO puede leerla. Una clave
    # foranea entre bases de microservicios distintos es acoplamiento de datos.
    organization_id = models.UUIDField(null=True, blank=True, db_index=True)
    store_id = models.UUIDField(null=True, blank=True, db_index=True)
    #: Codigo de producto especifico (ej. una denominacion concreta).
    product_code = models.CharField(max_length=64, blank=True, default="")

    # --- Calculo ------------------------------------------------------
    commission_type = models.CharField(max_length=16, choices=CommissionType.choices)

    #: Componente fijo, en centavos.
    fixed_cents = models.BigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Componente fijo en centavos. 1000 = $10.00 MXN.",
    )
    #: Componente porcentual en puntos porcentuales. 2.500 = 2.5%.
    percentage = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Puntos porcentuales. 2.500 = 2.5%.",
    )

    #: Cotas opcionales. Evitan que un porcentaje produzca una comision
    #: absurda en montos muy grandes o irrisoria en montos muy pequenos.
    min_cents = models.BigIntegerField(null=True, blank=True, validators=[MinValueValidator(0)])
    max_cents = models.BigIntegerField(null=True, blank=True, validators=[MinValueValidator(0)])

    # --- Vigencia -----------------------------------------------------
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)

    #: Prioridad manual para desempatar entre reglas del mismo ambito.
    priority = models.IntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by_id = models.UUIDField(null=True, blank=True)

    class Meta:
        verbose_name = "Regla de comision"
        verbose_name_plural = "Reglas de comision"
        ordering = ["-priority", "-created_at"]
        indexes = [
            models.Index(fields=["service_kind", "is_active"]),
            models.Index(fields=["store_id", "service_kind", "is_active"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_cents__isnull=True)
                | models.Q(min_cents__isnull=True)
                | models.Q(max_cents__gte=models.F("min_cents")),
                name="commission_max_gte_min",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True)
                | models.Q(valid_until__gt=models.F("valid_from")),
                name="commission_valid_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_commission_type_display()})"

    def clean(self) -> None:
        """Impide guardar reglas que no calculan nada."""
        if self.commission_type == CommissionType.FIXED and self.fixed_cents <= 0:
            raise ValidationError(
                {"fixed_cents": "Una comision fija debe ser mayor a cero."}
            )
        if self.commission_type == CommissionType.PERCENTAGE and self.percentage <= 0:
            raise ValidationError(
                {"percentage": "Una comision porcentual debe ser mayor a cero."}
            )
        if self.commission_type == CommissionType.MIXED and (
            self.fixed_cents <= 0 and self.percentage <= 0
        ):
            raise ValidationError(
                "Una comision mixta necesita al menos un componente mayor a cero."
            )

    # -- calculo -------------------------------------------------------

    def calculate(self, base: Money) -> Money:
        """Comision a cobrar sobre un monto base.

        >>> rule = CommissionRule(commission_type="MIXED",
        ...                       fixed_cents=500, percentage=Decimal("1.5"))
        >>> rule.calculate(Money.parse("300.00"))
        Money(cents=950, currency='MXN')
        """
        if self.commission_type == CommissionType.FIXED:
            amount = Money(self.fixed_cents, base.currency)
        elif self.commission_type == CommissionType.PERCENTAGE:
            amount = base.split_percentage(self.percentage)
        else:  # MIXED
            amount = Money(self.fixed_cents, base.currency) + base.split_percentage(
                self.percentage
            )

        if self.min_cents is not None and amount.cents < self.min_cents:
            amount = Money(self.min_cents, base.currency)
        if self.max_cents is not None and amount.cents > self.max_cents:
            amount = Money(self.max_cents, base.currency)

        return amount

    @property
    def specificity(self) -> int:
        """Que tan especifica es la regla. Mayor gana en la resolucion."""
        score = 0
        if self.organization_id:
            score += 1
        if self.store_id:
            score += 2
        if self.product_code:
            score += 4
        return score

    @property
    def is_currently_valid(self) -> bool:
        now = timezone.now()
        if not self.is_active or self.valid_from > now:
            return False
        return self.valid_until is None or self.valid_until > now

    def describe(self) -> str:
        """Descripcion legible, para mostrarla al dueno de la tienda."""
        parts = []
        if self.fixed_cents:
            parts.append(f"${Money(self.fixed_cents).amount:,.2f}")
        if self.percentage:
            parts.append(f"{self.percentage.normalize()}%")
        text = " + ".join(parts) or "sin comision"
        if self.min_cents is not None:
            text += f", minimo ${Money(self.min_cents).amount:,.2f}"
        if self.max_cents is not None:
            text += f", maximo ${Money(self.max_cents).amount:,.2f}"
        return text


class CommissionSplit(models.Model):
    """Como se reparte la comision cobrada.

    Los pesos son **enteros**, no porcentajes decimales. Motivo: el reparto
    usa ``Money.allocate()``, que reparte centavos por mayor residuo y
    garantiza que la suma de las partes sea EXACTAMENTE la comision cobrada.
    Con porcentajes decimales aparecerian diferencias de un centavo que en
    conciliacion contable son un problema real.

    Ejemplo: tienda 60, plataforma 30, reserva de proveedor 10.
    Sobre una comision de $10.00 -> $6.00 / $3.00 / $1.00.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    rule = models.OneToOneField(
        CommissionRule, on_delete=models.CASCADE, related_name="split"
    )

    store_weight = models.PositiveIntegerField(
        default=60, help_text="Peso de la ganancia de la tienda."
    )
    platform_weight = models.PositiveIntegerField(
        default=30, help_text="Peso de la comision de SAMY Cloud."
    )
    provider_weight = models.PositiveIntegerField(
        default=10, help_text="Peso reservado para el costo del proveedor."
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Reparto de comision"
        verbose_name_plural = "Repartos de comision"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(store_weight__gt=0)
                | models.Q(platform_weight__gt=0)
                | models.Q(provider_weight__gt=0),
                name="commission_split_nonzero",
            )
        ]

    def __str__(self) -> str:
        return (
            f"tienda {self.store_weight} / plataforma {self.platform_weight} "
            f"/ proveedor {self.provider_weight}"
        )

    def allocate(self, commission: Money) -> dict[str, Money]:
        """Reparte la comision. La suma es exactamente igual al total.

        >>> split = CommissionSplit(store_weight=1, platform_weight=1, provider_weight=1)
        >>> parts = split.allocate(Money(1000))
        >>> sum(p.cents for p in parts.values())
        1000
        """
        store, platform, provider = commission.allocate(
            [self.store_weight, self.platform_weight, self.provider_weight]
        )
        return {"store": store, "platform": platform, "provider": provider}
