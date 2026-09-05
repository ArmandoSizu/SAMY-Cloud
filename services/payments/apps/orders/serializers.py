"""Serializadores de la API de ordenes.

Los montos viajan SIEMPRE en centavos enteros por la API. Se incluye ademas
un campo ``*_display`` ya formateado para que el Core no tenga que replicar
la logica de presentacion de dinero y arriesgar una diferencia de redondeo
entre servicios.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.orders.models import Order, PaymentMethod, ServiceKind
from samy_common.money import Money


class CreateOrderSerializer(serializers.Serializer):
    organization_id = serializers.UUIDField()
    store_id = serializers.UUIDField()
    store_code = serializers.CharField(max_length=12)
    created_by_id = serializers.UUIDField()
    created_by_email = serializers.EmailField(required=False, allow_blank=True)

    service_kind = serializers.ChoiceField(choices=ServiceKind.choices)
    description = serializers.CharField(max_length=200)
    product_code = serializers.CharField(max_length=64, required=False, allow_blank=True)
    fulfillment_id = serializers.UUIDField(required=False, allow_null=True)

    base_cents = serializers.IntegerField(min_value=1)
    currency = serializers.CharField(max_length=3, default="MXN")
    metadata = serializers.DictField(required=False)

    def validate_base_cents(self, value: int) -> int:
        # Tope de seguridad: una recarga de mas de $50,000 en un punto de
        # venta de barrio es casi con certeza un error de captura (un cero de
        # mas). Se rechaza en vez de intentar cobrarlo.
        if value > 5_000_000:
            raise serializers.ValidationError(
                "El monto excede el limite por operacion ($50,000.00 MXN)."
            )
        return value


class StartPaymentSerializer(serializers.Serializer):
    store_id = serializers.UUIDField()
    actor_id = serializers.UUIDField()
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    #: Token de tarjeta generado por el tokenizador del proveedor EN EL
    #: NAVEGADOR. Nunca un numero de tarjeta.
    #:
    #: El limite de longitud y el rechazo de espacios no son cosmeticos: si
    #: alguien intentara mandar aqui un PAN con separadores, se corta en la
    #: frontera en vez de acabar en un log o en la base.
    card_token = serializers.RegexField(
        r"^[A-Za-z0-9_\-]{1,120}$",
        required=False,
        allow_blank=True,
        default="",
        error_messages={
            "invalid": "El token de la tarjeta no tiene un formato valido."
        },
    )

    def validate_card_token(self, value: str) -> str:
        """Rechaza cualquier cosa que parezca un numero de tarjeta.

        Un token de Conekta empieza por ``tok_``. Una cadena de 13 a 19
        digitos no es un token: es un PAN, y aceptarlo aunque fuera por error
        del cliente meteria datos de tarjeta en un sistema que esta disenado
        para no verlos nunca.
        """
        token = (value or "").strip()
        if not token:
            return ""
        solo_digitos = token.replace(" ", "").replace("-", "")
        if solo_digitos.isdigit() and 13 <= len(solo_digitos) <= 19:
            raise serializers.ValidationError(
                "Eso parece un numero de tarjeta. Aqui solo se acepta un token "
                "generado por el proveedor: el PAN nunca debe llegar al servidor."
            )
        return token


class ConfirmCashSerializer(serializers.Serializer):
    store_id = serializers.UUIDField()
    actor_id = serializers.UUIDField()
    amount_tendered_cents = serializers.IntegerField(min_value=1)


class CommissionEntrySerializer(serializers.Serializer):
    commission_cents = serializers.IntegerField()
    store_share_cents = serializers.IntegerField()
    platform_share_cents = serializers.IntegerField()
    provider_share_cents = serializers.IntegerField()
    rule_name = serializers.CharField()
    rule_description = serializers.CharField()


class OrderDetailSerializer(serializers.ModelSerializer):
    base_display = serializers.SerializerMethodField()
    commission_display = serializers.SerializerMethodField()
    total_display = serializers.SerializerMethodField()
    commission_entry = CommissionEntrySerializer(read_only=True)
    is_paid = serializers.BooleanField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "folio",
            "store_id",
            "created_by_id",
            "service_kind",
            "fulfillment_id",
            "description",
            "product_code",
            "currency",
            "base_cents",
            "commission_cents",
            "total_cents",
            "base_display",
            "commission_display",
            "total_display",
            "state",
            "state_reason",
            "payment_method",
            "provider_mode",
            "commission_entry",
            "is_paid",
            "is_expired",
            "created_at",
            "paid_at",
            "completed_at",
            "expires_at",
        ]
        read_only_fields = fields

    def get_base_display(self, obj: Order) -> str:
        return str(Money(obj.base_cents, obj.currency))

    def get_commission_display(self, obj: Order) -> str:
        return str(Money(obj.commission_cents, obj.currency))

    def get_total_display(self, obj: Order) -> str:
        return str(Money(obj.total_cents, obj.currency))


class OrderSummarySerializer(serializers.Serializer):
    date = serializers.DateField()
    total_operations = serializers.IntegerField()
    successful = serializers.IntegerField()
    failed = serializers.IntegerField()
    pending = serializers.IntegerField()
    under_review = serializers.IntegerField()
    gross_cents = serializers.IntegerField()
    commission_cents = serializers.IntegerField()
    store_earnings_cents = serializers.IntegerField()
    currency = serializers.CharField()
