"""Adaptador de tapi (agregador de pago de servicios en Latinoamerica).

ESTADO: **PENDIENTE DE CONTRATO COMERCIAL.**

Lo que se verifico (septiembre 2026, detalle en ``docs/api-integrations.md``):

* **CFE no tiene API publica** de consulta de adeudo ni de pago. No existe un
  portal para desarrolladores de CFE. Cualquier integracion pasa
  obligatoriamente por un agregador con convenio.
* **CAPDAM** es la Comision de Agua Potable, Drenaje y Alcantarillado de
  **Manzanillo, Colima** (no de Durango, como suele confundirse). Su sitio no
  expone API alguna y bloquea el acceso automatizado.
* tapi (tapi.la) publica que cubre luz, agua, gas, internet y seguros en
  Mexico, y tiene documentacion en developers.tapila.cloud, pero las
  credenciales se entregan bajo contrato.
* Alternativas equivalentes: Taecel, Sivetel y Arcus (adquirida por Mastercard
  en 2021). Ninguna es de auto-servicio.

Por que este adaptador no llama a endpoints "probables":

Escribir un cliente contra una API cuya documentacion no se ha leido produce
codigo que compila, pasa un test con respuestas inventadas y falla el dia que
se conecta de verdad. Peor aun en pagos de servicios: un campo mal mapeado
significa pagarle el recibo a otra persona. Aqui los metodos levantan
``ProviderNotConfigured`` con la lista exacta de lo que falta.

**El scraping del portal de CFE no es una opcion.** Ademas de ser fragil y de
violar sus terminos, no da confirmacion fiable de pago, que es justamente lo
que un sistema de dinero necesita.

PARA ACTIVARLO SE NECESITA:
  1. Contacto comercial con tapi (o Taecel / Sivetel / Arcus)
  2. Contrato firmado y alta como comercio
  3. Documentacion oficial de su API
  4. Credenciales de sandbox y de produccion
  5. Fondeo prepagado
  6. **La especificacion del codigo de barras del recibo de CFE**, que no es
     publica y debe pedirse como parte del due diligence
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.providers.base import (
    BillerCatalogItem,
    BillerProvider,
    BillInquiry,
    BillPaymentRequest,
    BillPaymentResult,
)
from apps.providers.registry import biller_registry
from samy_common.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderStatus,
)
from samy_common.providers.exceptions import ProviderNotConfigured

PENDING_REQUIREMENTS: tuple[str, ...] = (
    "Contrato comercial firmado con un agregador autorizado",
    "Alta como comercio y validacion de documentos (RFC, acta constitutiva)",
    "Documentacion oficial de la API (no es publica)",
    "Credenciales TAPI_API_KEY y TAPI_API_SECRET",
    "Cuenta fondeada con saldo prepagado",
    "Especificacion del codigo de barras del recibo de CFE",
)


@dataclass(frozen=True, slots=True)
class TapiConfig:
    api_key: str = ""
    api_secret: str = ""


@biller_registry.register
class TapiProvider(BillerProvider):
    """Agregador de pago de servicios. Requiere contrato comercial."""

    slug = "tapi"
    display_name = "tapi"
    capabilities = frozenset(
        {ProviderCapability.BILL_INQUIRY, ProviderCapability.BILL_PAYMENT}
    )
    required_settings = ("TAPI_API_KEY", "TAPI_API_SECRET")
    requires_commercial_contract = True
    documentation_url = "https://developers.tapila.cloud/docs/"

    def check_health(self) -> ProviderHealth:
        missing = [
            name
            for name, value in (
                ("TAPI_API_KEY", self.config.api_key),
                ("TAPI_API_SECRET", self.config.api_secret),
            )
            if not value
        ]

        if missing:
            return ProviderHealth(
                status=ProviderStatus.PENDING_CONTRACT,
                detail=(
                    "El pago de servicios requiere un agregador con convenio. "
                    "CFE y CAPDAM no exponen API publica: no existe forma "
                    "legitima de integrarlos directamente. "
                    "Ver docs/bill-payments.md."
                ),
                missing_requirements=PENDING_REQUIREMENTS,
            )

        return ProviderHealth(
            status=ProviderStatus.PENDING_CONTRACT,
            detail=(
                "Hay credenciales configuradas, pero el adaptador aun no "
                "implementa los endpoints del agregador: falta su documentacion "
                "oficial. No se implementa a partir de suposiciones."
            ),
            missing_requirements=(
                "Documentacion oficial de la API del agregador",
                "Implementacion de fetch_billers, inquire y pay",
            ),
        )

    def _not_ready(self) -> ProviderNotConfigured:
        return ProviderNotConfigured(
            provider=self.slug,
            message=(
                "La integracion de pago de servicios no esta activa. "
                "Requiere contrato con un agregador autorizado."
            ),
            missing_requirements=PENDING_REQUIREMENTS,
            status=ProviderStatus.PENDING_CONTRACT,
        )

    def fetch_billers(self) -> list[BillerCatalogItem]:
        raise self._not_ready()

    def inquire(self, *, biller_id: str, reference: str) -> BillInquiry:
        raise self._not_ready()

    def pay(self, request: BillPaymentRequest) -> BillPaymentResult:
        raise self._not_ready()

    def get_payment_status(self, provider_reference: str) -> BillPaymentResult:
        raise self._not_ready()
