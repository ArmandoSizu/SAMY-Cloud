"""Serializadores de pagos de recibos.

La referencia COMPLETA nunca sale en una respuesta: solo la enmascarada.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.fulfillment.models import BillPaymentFulfillment
from samy_common.money import Money


class CreateBillPaymentSerializer(serializers.Serializer):
    organization_id = serializers.UUIDField()
    store_id = serializers.UUIDField()
    requested_by_id = serializers.UUIDField()
    biller_id = serializers.UUIDField()
    reference = serializers.CharField(max_length=64)
    amount_cents = serializers.IntegerField(min_value=1)
    customer_name = serializers.CharField(required=False, allow_blank=True, max_length=160)
    period = serializers.CharField(required=False, allow_blank=True, max_length=60)


class BillPaymentSerializer(serializers.ModelSerializer):
    amount_display = serializers.SerializerMethodField()

    class Meta:
        model = BillPaymentFulfillment
        fields = [
            "id", "order_id", "store_id", "biller_name",
            # 'reference' NO se expone. Solo la enmascarada.
            "reference_masked", "customer_name", "period", "due_date",
            "currency", "amount_cents", "amount_display",
            "state", "state_reason", "provider_slug", "provider_mode",
            "biller_reference", "failure_reason",
            "created_at", "sent_at", "completed_at",
        ]
        read_only_fields = fields

    def get_amount_display(self, obj) -> str:
        return str(Money(obj.amount_cents, obj.currency))
