"""Serializadores del catalogo."""

from __future__ import annotations

from rest_framework import serializers

from apps.catalog.models import Operator, TopupProduct
from samy_common.money import Money


class TopupProductSerializer(serializers.ModelSerializer):
    amount_display = serializers.SerializerMethodField()
    is_open_amount = serializers.BooleanField(read_only=True)

    class Meta:
        model = TopupProduct
        fields = [
            "id",
            "label",
            "description",
            "currency",
            "amount_cents",
            "amount_display",
            "min_amount_cents",
            "max_amount_cents",
            "is_open_amount",
            "is_data_package",
            "validity_days",
        ]
        read_only_fields = fields

    def get_amount_display(self, obj: TopupProduct) -> str:
        if obj.amount_cents is None:
            return "Monto libre"
        return str(Money(obj.amount_cents, obj.currency))


class OperatorSerializer(serializers.ModelSerializer):
    products = TopupProductSerializer(many=True, read_only=True)

    class Meta:
        model = Operator
        fields = [
            "id",
            "name",
            "slug",
            "logo_url",
            "supports_data_packages",
            "display_order",
            "products",
        ]
        read_only_fields = fields
