"""Serializadores de recargas.

El numero telefonico COMPLETO nunca sale en una respuesta: solo el enmascarado.
El completo existe unicamente para llamar al proveedor.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.fulfillment.models import TopupFulfillment
from samy_common.money import Money


class CreateTopupSerializer(serializers.Serializer):
    organization_id = serializers.UUIDField()
    store_id = serializers.UUIDField()
    requested_by_id = serializers.UUIDField()
    product_id = serializers.UUIDField()
    phone = serializers.CharField(max_length=25)
    #: Solo para productos de monto libre.
    amount_cents = serializers.IntegerField(required=False, min_value=1)


class TopupFulfillmentSerializer(serializers.ModelSerializer):
    amount_display = serializers.SerializerMethodField()

    class Meta:
        model = TopupFulfillment
        fields = [
            "id",
            "order_id",
            "store_id",
            "operator_name",
            "product_label",
            # phone_e164 NO se expone. Solo el enmascarado.
            "phone_masked",
            "currency",
            "amount_cents",
            "amount_display",
            "state",
            "state_reason",
            "provider_slug",
            "provider_mode",
            "operator_reference",
            "failure_reason",
            "created_at",
            "sent_at",
            "completed_at",
        ]
        read_only_fields = fields

    def get_amount_display(self, obj: TopupFulfillment) -> str:
        return str(Money(obj.amount_cents, obj.currency))
