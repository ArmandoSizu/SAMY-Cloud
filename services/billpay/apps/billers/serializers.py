"""Serializadores de servicios pagables."""

from __future__ import annotations

from rest_framework import serializers

from apps.billers.models import Biller


class BillerSerializer(serializers.ModelSerializer):
    reference_label = serializers.SerializerMethodField()
    reference_help_text = serializers.SerializerMethodField()
    reference_max_length = serializers.SerializerMethodField()
    reference_numeric_only = serializers.SerializerMethodField()
    barcode_formats = serializers.SerializerMethodField()
    reference_is_specified = serializers.SerializerMethodField()

    class Meta:
        model = Biller
        fields = [
            "id", "name", "slug", "category", "coverage_note", "state_code",
            "logo_url", "supports_inquiry", "supports_partial_payment",
            "reference_label", "reference_help_text", "reference_max_length",
            "reference_numeric_only", "barcode_formats", "reference_is_specified",
        ]
        read_only_fields = fields

    def _fmt(self, obj):
        return getattr(obj, "reference_format", None)

    def get_reference_label(self, obj) -> str:
        fmt = self._fmt(obj)
        return fmt.label if fmt else "Referencia"

    def get_reference_help_text(self, obj) -> str:
        fmt = self._fmt(obj)
        return fmt.help_text if fmt else ""

    def get_reference_max_length(self, obj) -> int:
        fmt = self._fmt(obj)
        return fmt.max_length if fmt else 40

    def get_reference_numeric_only(self, obj) -> bool:
        fmt = self._fmt(obj)
        return fmt.numeric_only if fmt else True

    def get_barcode_formats(self, obj) -> list:
        fmt = self._fmt(obj)
        return list(fmt.barcode_formats) if fmt and fmt.barcode_formats else []

    def get_reference_is_specified(self, obj) -> bool:
        """Si la especificacion de la referencia esta verificada.

        Cuando es False, la UI advierte al cajero que revise la referencia
        contra el recibo impreso: solo se valida la longitud.
        """
        fmt = self._fmt(obj)
        return bool(fmt and fmt.is_specified)


class InquirySerializer(serializers.Serializer):
    biller_id = serializers.UUIDField()
    reference = serializers.CharField(max_length=64)
    store_id = serializers.UUIDField()
    requested_by_id = serializers.UUIDField(required=False, allow_null=True)
    source = serializers.ChoiceField(choices=["scan", "manual"], default="manual")
