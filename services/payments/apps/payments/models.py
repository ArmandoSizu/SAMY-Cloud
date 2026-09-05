"""Intentos de cobro y tokens temporales de pago.

Un ``PaymentAttempt`` por cada intento de cobrar una orden. Se separan de la
orden porque una misma orden puede tener varios intentos: la tarjeta se
rechaza, el cliente paga en efectivo; o el QR expira y se genera otro. Meter
esto en la orden significaria perder el rastro de lo que se intento.

Cada intento guarda su propio ``provider_mode``, de modo que en el historial
se distingue sin ambiguedad un cobro de sandbox de uno real.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import TYPE_CHECKING

import structlog
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from samy_common.money import Money

if TYPE_CHECKING:  # pragma: no cover
    from apps.providers.base import PaymentResult

log = structlog.get_logger("payments")


class PaymentAttemptStatus(models.TextChoices):
    INITIATED = "INITIATED", "Iniciado"
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER", "Esperando al cliente"
    CONFIRMED = "CONFIRMED", "Confirmado"
    DECLINED = "DECLINED", "Rechazado"
    EXPIRED = "EXPIRED", "Expirado"
    #: Resultado desconocido: hay que consultar al proveedor, no suponer.
    INDETERMINATE = "INDETERMINATE", "Indeterminado"


class PaymentAttempt(models.Model):
    """Un intento de cobrar una orden."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        "orders.Order", on_delete=models.CASCADE, related_name="attempts"
    )

    provider_slug = models.CharField(max_length=40, db_index=True)
    #: SANDBOX o PRODUCTION. Se guarda por intento, no solo por orden.
    provider_mode = models.CharField(max_length=16)
    method = models.CharField(max_length=16)

    amount_cents = models.BigIntegerField(validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, default="MXN")

    status = models.CharField(
        max_length=24,
        choices=PaymentAttemptStatus.choices,
        default=PaymentAttemptStatus.INITIATED,
        db_index=True,
    )

    #: Identificador del cargo en el proveedor. Es la clave de conciliacion.
    provider_reference = models.CharField(max_length=128, blank=True, default="", db_index=True)
    #: Clave enviada al proveedor para que no cobre dos veces.
    idempotency_key = models.CharField(max_length=255, unique=True)

    checkout_url = models.URLField(max_length=500, blank=True, default="")
    declined_reason = models.CharField(max_length=255, blank=True, default="")

    #: Respuesta del proveedor, ya saneada de datos de tarjeta.
    raw_response = models.JSONField(default=dict, blank=True)

    correlation_id = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Intento de pago"
        verbose_name_plural = "Intentos de pago"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["provider_slug", "provider_reference"]),
            models.Index(
                fields=["status", "created_at"],
                name="attempt_needs_recon_idx",
                condition=models.Q(status="INDETERMINATE"),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider_slug} {self.status} {Money(self.amount_cents, self.currency)}"

    @property
    def amount(self) -> Money:
        return Money(self.amount_cents, self.currency)

    # -- transiciones del intento --------------------------------------

    def apply_provider_result(self, result: "PaymentResult") -> None:
        from apps.providers.base import PaymentOutcome

        self.provider_reference = result.provider_reference[:128]
        self.provider_mode = result.provider_mode
        self.checkout_url = result.checkout_url[:500]
        self.raw_response = result.raw_response

        if result.outcome == PaymentOutcome.CONFIRMED:
            self.status = PaymentAttemptStatus.CONFIRMED
            self.confirmed_at = timezone.now()
        elif result.outcome == PaymentOutcome.PENDING:
            self.status = PaymentAttemptStatus.AWAITING_CUSTOMER
        elif result.outcome == PaymentOutcome.DECLINED:
            self.status = PaymentAttemptStatus.DECLINED
            self.declined_reason = result.declined_reason[:255]
        else:
            self.status = PaymentAttemptStatus.INDETERMINATE

        self.save(
            update_fields=[
                "provider_reference",
                "provider_mode",
                "checkout_url",
                "raw_response",
                "status",
                "confirmed_at",
                "declined_reason",
                "updated_at",
            ]
        )

    def mark_confirmed(self, provider_reference: str) -> None:
        self.status = PaymentAttemptStatus.CONFIRMED
        self.confirmed_at = timezone.now()
        if provider_reference:
            self.provider_reference = provider_reference[:128]
        self.save(
            update_fields=["status", "confirmed_at", "provider_reference", "updated_at"]
        )

    def mark_failed(self, reason: str) -> None:
        self.status = PaymentAttemptStatus.DECLINED
        self.declined_reason = reason[:255]
        self.save(update_fields=["status", "declined_reason", "updated_at"])

    def mark_indeterminate(self, reason: str) -> None:
        self.status = PaymentAttemptStatus.INDETERMINATE
        self.declined_reason = reason[:255]
        self.save(update_fields=["status", "declined_reason", "updated_at"])


class PaymentQrToken(models.Model):
    """Token temporal de cobro, presentado como QR en el mostrador.

    Sustituye al papelito con el numero de cuenta pegado en la pared: cada
    venta genera su propio codigo, atado a esa orden y a ese monto.

    Propiedades de seguridad:

    * **De un solo uso.** ``consumed_at`` con restriccion en base de datos.
    * **Expira** entre 3 y 5 minutos (configurable). Un QR eterno es un cobro
      que cualquiera puede reutilizar.
    * **No adivinable.** 32 bytes de ``secrets.token_urlsafe``.
    * **Se guarda solo el hash**, nunca el token en claro. Si alguien lee la
      base de datos no puede reconstruir tokens vigentes, igual que con las
      contrasenas.
    * Lleva ``nonce`` propio, de modo que dos ordenes del mismo monto en el
      mismo segundo produzcan codigos distintos.

    ADVERTENCIA IMPORTANTE: este token identifica una orden dentro de SAMY
    Cloud. **No es** un QR de cobro interbancario. Un QR real de SPEI o CoDi
    solo puede emitirlo una institucion financiera regulada ante Banxico. Este
    token sirve para que el cliente abra el checkout de la orden en su
    telefono; el cobro efectivo lo confirma el proveedor de pagos, no este QR.
    Ver ``docs/payments.md``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        "orders.Order", on_delete=models.CASCADE, related_name="qr_tokens"
    )

    #: SHA-256 del token. El token en claro solo existe en la respuesta HTTP
    #: que lo genera y en el QR que ve el cliente.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    nonce = models.CharField(max_length=32)

    amount_cents = models.BigIntegerField(validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, default="MXN")
    store_id = models.UUIDField(db_index=True)

    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    consumed_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "Token QR de cobro"
        verbose_name_plural = "Tokens QR de cobro"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["expires_at"],
                name="qr_active_idx",
                condition=models.Q(consumed_at__isnull=True),
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="qr_expires_after_creation",
            )
        ]

    def __str__(self) -> str:
        return f"QR {self.order_id} ({'usado' if self.consumed_at else 'vigente'})"

    # -- creacion y validacion ------------------------------------------

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @classmethod
    def issue(cls, *, order, ttl_seconds: int) -> tuple["PaymentQrToken", str]:
        """Emite un token nuevo. Devuelve el registro y el token EN CLARO.

        El token en claro se devuelve una sola vez y no se persiste. Quien
        llama lo pinta en el QR y lo descarta.
        """
        raw_token = secrets.token_urlsafe(32)
        token = cls.objects.create(
            order=order,
            token_hash=cls.hash_token(raw_token),
            nonce=secrets.token_hex(16),
            amount_cents=order.total_cents,
            currency=order.currency,
            store_id=order.store_id,
            expires_at=timezone.now() + timezone.timedelta(seconds=ttl_seconds),
        )
        log.info(
            "qr_token_issued",
            order_id=str(order.id),
            folio=order.folio,
            ttl_seconds=ttl_seconds,
        )
        return token, raw_token

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def is_valid(self) -> bool:
        return not self.is_expired and not self.is_consumed

    @property
    def seconds_remaining(self) -> int:
        return max(0, int((self.expires_at - timezone.now()).total_seconds()))

    def consume(self, ip: str | None = None) -> bool:
        """Marca el token como usado. Devuelve False si ya no era valido.

        El ``UPDATE ... WHERE consumed_at IS NULL`` es una operacion atomica en
        la base de datos: si dos peticiones intentan consumir el mismo token a
        la vez, solo una afecta una fila. Esto es lo que impide reutilizarlo,
        no una comprobacion en Python.
        """
        if self.is_expired:
            return False
        updated = PaymentQrToken.objects.filter(
            pk=self.pk, consumed_at__isnull=True, expires_at__gt=timezone.now()
        ).update(consumed_at=timezone.now(), consumed_ip=ip)
        if updated:
            self.refresh_from_db(fields=["consumed_at", "consumed_ip"])
            return True
        return False
