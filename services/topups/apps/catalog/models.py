"""Catalogo de recargas, sincronizado desde el proveedor.

Requisito literal del producto:

    "No hardcodees catalogos como si fueran eternos. NO debes permitir
     arbitrariamente $75, $100, $150 si el proveedor no ofrece esa
     denominacion."

Como se cumple:

* La tabla se llena **exclusivamente** desde ``provider.fetch_catalog()``.
  No hay migracion de datos con denominaciones, ni lista en el codigo, ni
  valores por defecto.
* Un producto que desaparece del catalogo del proveedor se marca inactivo,
  **no se borra**: las ordenes historicas lo referencian y borrarlo dejaria
  comprobantes sin explicacion.
* Cada producto guarda cuando se vio por ultima vez. Si la sincronizacion
  lleva mucho sin correr, la UI puede advertir que el catalogo esta viejo en
  vez de vender a ciegas.
* Si nunca se ha sincronizado, la pantalla de recargas muestra un estado vacio
  explicando que falta configurar el proveedor. **No muestra denominaciones
  inventadas.**
"""

from __future__ import annotations

import uuid

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from samy_common.money import Money


class Operator(models.Model):
    """Compania telefonica, tal como la publica el proveedor."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    provider_slug = models.CharField(max_length=40, db_index=True)
    #: Identificador del operador EN EL PROVEEDOR. No es un codigo nuestro.
    provider_operator_id = models.CharField(max_length=64)

    name = models.CharField(max_length=100)
    #: Nombre normalizado para agrupar y ordenar (telcel, movistar, att...).
    slug = models.SlugField(max_length=60)
    country_code = models.CharField(max_length=2, default="MX")
    logo_url = models.URLField(max_length=500, blank=True, default="")

    supports_data_packages = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True, db_index=True)
    #: Orden de aparicion en la pantalla del cajero. Los operadores mas
    #: vendidos primero: en un mostrador, dos toques menos importan.
    display_order = models.PositiveSmallIntegerField(default=100)

    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Operador"
        verbose_name_plural = "Operadores"
        ordering = ["display_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_slug", "provider_operator_id"],
                name="operator_unique_per_provider",
            )
        ]
        indexes = [models.Index(fields=["is_active", "display_order"])]

    def __str__(self) -> str:
        return self.name


class TopupProduct(models.Model):
    """Una denominacion o paquete concreto que el proveedor vende hoy."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    operator = models.ForeignKey(
        Operator, on_delete=models.CASCADE, related_name="products"
    )

    provider_slug = models.CharField(max_length=40, db_index=True)
    #: Identificador del producto EN EL PROVEEDOR. Es lo que se le envia al
    #: solicitar la recarga.
    provider_product_id = models.CharField(max_length=128)

    label = models.CharField(max_length=160)
    description = models.CharField(max_length=300, blank=True, default="")

    currency = models.CharField(max_length=3, default="MXN")
    #: Monto fijo en centavos. Nulo cuando el producto acepta monto libre.
    amount_cents = models.BigIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    #: Cotas para productos de monto libre.
    min_amount_cents = models.BigIntegerField(null=True, blank=True)
    max_amount_cents = models.BigIntegerField(null=True, blank=True)

    is_data_package = models.BooleanField(default=False)
    validity_days = models.PositiveSmallIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)
    #: Ultima vez que el proveedor lo devolvio en su catalogo. Si esta viejo,
    #: el producto probablemente ya no existe.
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)

    #: Respuesta cruda del proveedor, para depurar diferencias de catalogo.
    raw = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Producto de recarga"
        verbose_name_plural = "Productos de recarga"
        ordering = ["operator__display_order", "amount_cents", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_slug", "provider_product_id"],
                name="product_unique_per_provider",
            ),
            # O tiene monto fijo, o tiene rango. Nunca ninguno de los dos:
            # un producto sin monto no se puede vender.
            models.CheckConstraint(
                condition=models.Q(amount_cents__isnull=False)
                | (
                    models.Q(min_amount_cents__isnull=False)
                    & models.Q(max_amount_cents__isnull=False)
                ),
                name="product_has_amount_or_range",
            ),
        ]
        indexes = [
            models.Index(fields=["operator", "is_active", "amount_cents"]),
            models.Index(fields=["is_active", "last_seen_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.operator.name} - {self.label}"

    @property
    def amount(self) -> Money | None:
        return Money(self.amount_cents, self.currency) if self.amount_cents else None

    @property
    def is_open_amount(self) -> bool:
        return self.amount_cents is None

    def validate_amount(self, amount: Money) -> None:
        """Verifica que el monto solicitado sea vendible para este producto.

        Se ejecuta en el SERVIDOR antes de crear la orden. Aunque la UI solo
        muestre montos validos, cualquiera puede enviar otro por la API.
        """
        from django.core.exceptions import ValidationError

        if self.amount_cents is not None:
            if amount.cents != self.amount_cents:
                raise ValidationError(
                    f"Este producto solo se vende por {Money(self.amount_cents, self.currency)}."
                )
            return

        if self.min_amount_cents is not None and amount.cents < self.min_amount_cents:
            raise ValidationError(
                f"El monto minimo es {Money(self.min_amount_cents, self.currency)}."
            )
        if self.max_amount_cents is not None and amount.cents > self.max_amount_cents:
            raise ValidationError(
                f"El monto maximo es {Money(self.max_amount_cents, self.currency)}."
            )


class CatalogSyncRun(models.Model):
    """Bitacora de cada sincronizacion del catalogo.

    Sirve para dos cosas concretas: saber si el catalogo esta al dia antes de
    vender, y diagnosticar por que desaparecio un producto que el cajero
    esperaba encontrar.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    provider_slug = models.CharField(max_length=40, db_index=True)
    provider_mode = models.CharField(max_length=16, blank=True, default="")

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)

    operators_found = models.PositiveIntegerField(default=0)
    #: Operadores que el proveedor devolvio pero que NO se pueden vender.
    #:
    #: El caso real: operadores que solo publican precio en la moneda del
    #: monedero (USD) y no en pesos. Ponerles precio exigiria aplicar nuestro
    #: propio tipo de cambio, es decir inventar un precio. Se descartan, pero
    #: el numero queda a la vista: si el catalogo trae menos companias de las
    #: esperadas, aqui esta la explicacion en vez de un misterio.
    operators_skipped = models.PositiveIntegerField(default=0)
    products_found = models.PositiveIntegerField(default=0)
    products_created = models.PositiveIntegerField(default=0)
    products_updated = models.PositiveIntegerField(default=0)
    products_deactivated = models.PositiveIntegerField(default=0)

    error_message = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "Sincronizacion de catalogo"
        verbose_name_plural = "Sincronizaciones de catalogo"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["provider_slug", "-started_at"])]

    def __str__(self) -> str:
        estado = "OK" if self.succeeded else "FALLIDA"
        return f"{self.provider_slug} {self.started_at:%Y-%m-%d %H:%M} [{estado}]"

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()
