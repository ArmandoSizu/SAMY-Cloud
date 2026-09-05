"""Catalogo de servicios (billers) y reglas de referencia.

Un ``Biller`` es una empresa a la que se le puede pagar: CFE, CAPDAM, gas,
internet. Igual que con las recargas, el catalogo se sincroniza desde el
agregador y no se codifica a mano.

La diferencia con las recargas es la **referencia**: cada biller identifica al
cliente con un formato propio (numero de servicio, contrato, cuenta). Validar
ese formato ANTES de cobrar evita el peor error posible en este flujo: pagarle
el recibo a otra persona, que es dinero perdido y practicamente irrecuperable.

Nota importante y verificada (septiembre 2026): **CFE no publica una
especificacion abierta del codigo de barras de su recibo.** Lo unico
verificable publicamente es que el numero de servicio tiene 12 digitos. Por eso
``ReferenceFormat`` es configurable por biller y NO trae un parser codificado
del recibo de CFE: se rellena con la especificacion que entregue el agregador
al firmar el contrato. Inventar el formato produciria un parser que parece
funcionar y falla con recibos reales.
"""

from __future__ import annotations

import re
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class BillerCategory(models.TextChoices):
    ELECTRICITY = "ELECTRICITY", "Luz"
    WATER = "WATER", "Agua"
    GAS = "GAS", "Gas"
    TELECOM = "TELECOM", "Telefonia e internet"
    TV = "TV", "Television"
    OTHER = "OTHER", "Otros"


class Biller(models.Model):
    """Una empresa a la que se le puede pagar un servicio."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    provider_slug = models.CharField(max_length=40, db_index=True)
    #: Identificador del biller EN EL AGREGADOR.
    provider_biller_id = models.CharField(max_length=64)

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    category = models.CharField(max_length=20, choices=BillerCategory.choices)

    #: Cobertura geografica. CAPDAM, por ejemplo, opera solo en Manzanillo,
    #: Colima: mostrarlo a una tienda de otro estado seria ofrecer algo que no
    #: le sirve a ningun cliente.
    coverage_note = models.CharField(max_length=160, blank=True, default="")
    state_code = models.CharField(max_length=4, blank=True, default="")

    logo_url = models.URLField(max_length=500, blank=True, default="")

    #: Si el agregador permite consultar el adeudo antes de cobrar. Cuando es
    #: False, el cajero captura el monto del recibo impreso.
    supports_inquiry = models.BooleanField(default=False)
    #: Si acepta pagos parciales. Determina si se valida monto exacto.
    supports_partial_payment = models.BooleanField(default=False)

    min_amount_cents = models.BigIntegerField(null=True, blank=True)
    max_amount_cents = models.BigIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveSmallIntegerField(default=100)

    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Servicio"
        verbose_name_plural = "Servicios"
        ordering = ["display_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_slug", "provider_biller_id"],
                name="biller_unique_per_provider",
            )
        ]
        indexes = [models.Index(fields=["is_active", "category", "display_order"])]

    def __str__(self) -> str:
        return self.name


class ReferenceFormat(models.Model):
    """Como se valida la referencia de un biller.

    Se guarda como configuracion y no como codigo porque cada biller es
    distinto y porque los formatos cambian. Lo que aqui se define es lo que
    entregue el agregador en su documentacion: **no se inventa**.

    ``validation_regex`` vacio significa "no tenemos la especificacion": en ese
    caso solo se comprueba la longitud, y la UI advierte al cajero que revise
    la referencia contra el recibo impreso antes de cobrar.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    biller = models.OneToOneField(
        Biller, on_delete=models.CASCADE, related_name="reference_format"
    )

    label = models.CharField(
        max_length=80,
        default="Referencia",
        help_text="Como se llama en el recibo: 'Numero de servicio', 'Contrato'...",
    )
    help_text = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Donde encontrarlo en el recibo. Se muestra al cajero.",
    )

    min_length = models.PositiveSmallIntegerField(default=1)
    max_length = models.PositiveSmallIntegerField(default=40)
    #: Vacio = no hay especificacion oficial disponible todavia.
    validation_regex = models.CharField(max_length=200, blank=True, default="")
    #: Si solo admite digitos, el celular abre el teclado numerico.
    numeric_only = models.BooleanField(default=True)

    #: Simbologias que puede traer el codigo de barras del recibo. Se declara
    #: por biller porque no todos usan la misma y suponerlo hace que el lector
    #: falle sin explicacion.
    barcode_formats = models.JSONField(
        default=list,
        blank=True,
        help_text="Ej. ['code_128', 'itf']. Vacio = se prueban todas.",
    )
    #: Si el codigo de barras trae mas datos que la referencia (monto, fecha),
    #: aqui va como extraerla. Vacio = el codigo ES la referencia.
    barcode_extraction_regex = models.CharField(max_length=200, blank=True, default="")

    example = models.CharField(max_length=60, blank=True, default="")
    #: Documenta de donde salio esta especificacion. Si dice "pendiente", nadie
    #: debe asumir que esta verificada.
    specification_source = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="De donde se obtuvo. 'Pendiente' si aun no hay documentacion oficial.",
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Formato de referencia"
        verbose_name_plural = "Formatos de referencia"

    def __str__(self) -> str:
        return f"{self.biller.name}: {self.label}"

    @property
    def is_specified(self) -> bool:
        """Si tenemos una especificacion real o solo una validacion generica."""
        return bool(self.validation_regex)

    def validate(self, reference: str) -> str:
        """Valida y normaliza una referencia. Levanta si no cumple.

        Esta validacion es de USABILIDAD: atrapa errores de captura antes de
        cobrar. **No sustituye a la validacion del agregador**, que es la unica
        que sabe si la referencia existe de verdad. Por eso el flujo siempre
        consulta al agregador antes de aceptar el pago cuando este soporta
        consulta.
        """
        cleaned = re.sub(r"[\s\-]", "", (reference or "").strip())

        if not cleaned:
            raise ValidationError(f"Captura {self.label.lower()}.")

        if self.numeric_only and not cleaned.isdigit():
            raise ValidationError(f"{self.label} solo debe contener numeros.")

        if not (self.min_length <= len(cleaned) <= self.max_length):
            if self.min_length == self.max_length:
                raise ValidationError(
                    f"{self.label} debe tener exactamente {self.min_length} digitos. "
                    f"Capturaste {len(cleaned)}."
                )
            raise ValidationError(
                f"{self.label} debe tener entre {self.min_length} y "
                f"{self.max_length} caracteres. Capturaste {len(cleaned)}."
            )

        if self.validation_regex and not re.fullmatch(self.validation_regex, cleaned):
            raise ValidationError(
                f"{self.label} no tiene el formato esperado. "
                f"Revisala contra el recibo."
            )

        return cleaned

    def extract_from_barcode(self, barcode_value: str) -> str:
        """Obtiene la referencia a partir del codigo de barras leido.

        Sin regla de extraccion configurada, se devuelve el codigo tal cual y
        la validacion normal decide. Inventar una regla de extraccion produce
        referencias mal recortadas, es decir, pagos a la cuenta equivocada.
        """
        value = (barcode_value or "").strip()
        if not self.barcode_extraction_regex:
            return value

        match = re.search(self.barcode_extraction_regex, value)
        if not match:
            raise ValidationError(
                "El codigo leido no corresponde a un recibo de este servicio. "
                "Verifica el servicio seleccionado o captura la referencia manualmente."
            )
        return match.group(1) if match.groups() else match.group(0)
