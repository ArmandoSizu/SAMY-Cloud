"""Adaptador de Taecel (distribuidor mexicano de tiempo aire).

ESTADO: **PENDIENTE DE CONTRATO COMERCIAL.**

Que se verifico (septiembre 2026):

* Taecel opera como distribuidor autorizado con cobertura de Telcel, Movistar,
  AT&T, Unefon, Iusacell, Weex, Virgin y Oui, ademas de pago de servicios.
* **No publica su documentacion de API.** El acceso tecnico se entrega solo
  despues de un "levantamiento tecnologico": un formato que se solicita por
  correo, se llena y revisa un ingeniero de su equipo.
* Ofrecen codigos de prueba una vez dada de alta la cuenta.
* El modelo comercial es de saldo prepagado: se compra saldo por adelantado y
  cada recarga lo descuenta.

Por que existe este archivo si no se puede usar todavia:

1. Deja escrito y revisable **exactamente que falta** para activarlo, en el
   mismo lugar donde vivira el codigo.
2. Obliga a que la arquitectura soporte de verdad varios proveedores. Un
   contrato con un solo adaptador implementado suele resultar, al llegar el
   segundo, en un contrato mal disenado.
3. Cuando lleguen las credenciales y la documentacion, el trabajo es rellenar
   los metodos, no rehacer el microservicio.

Lo que este archivo **no** hace: inventar la forma de las peticiones. Los
metodos levantan ``ProviderNotConfigured`` en vez de llamar a endpoints
supuestos. Escribir un cliente contra una API que no se ha leido produce
codigo que parece funcional y falla en produccion.

PARA ACTIVARLO SE NECESITA:
  1. Alta como distribuidor en https://taecel.com/portal/integracion-web-services
  2. Completar el levantamiento tecnologico que envian por correo
  3. Recibir la documentacion oficial de su web service
  4. Recibir TAECEL_KEY y TAECEL_NIP
  5. Fondear la cuenta con saldo prepagado
  6. Implementar los metodos siguiendo SU documentacion, no suposiciones
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.providers.base import (
    CatalogProduct,
    TopupProvider,
    TopupRequest,
    TopupResult,
)
from apps.providers.registry import topup_registry
from samy_common.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderStatus,
)
from samy_common.providers.exceptions import ProviderNotConfigured

#: Lo que hay que conseguir antes de poder operar. Se muestra tal cual en el
#: panel de administracion de la plataforma.
PENDING_REQUIREMENTS: tuple[str, ...] = (
    "Alta como distribuidor autorizado en Taecel",
    "Levantamiento tecnologico completado y aprobado",
    "Documentacion oficial del web service (no es publica)",
    "Credenciales TAECEL_KEY y TAECEL_NIP",
    "Saldo prepagado fondeado en la cuenta",
)


@dataclass(frozen=True, slots=True)
class TaecelConfig:
    key: str = ""
    nip: str = ""


@topup_registry.register
class TaecelProvider(TopupProvider):
    """Distribuidor mexicano de tiempo aire. Requiere contrato comercial."""

    slug = "taecel"
    display_name = "Taecel"
    capabilities = frozenset(
        {ProviderCapability.AIRTIME_TOPUP, ProviderCapability.CATALOG_SYNC}
    )
    required_settings = ("TAECEL_KEY", "TAECEL_NIP")
    requires_commercial_contract = True
    documentation_url = "https://taecel.com/portal/integracion-web-services"

    def check_health(self) -> ProviderHealth:
        missing = [
            name
            for name, value in (
                ("TAECEL_KEY", self.config.key),
                ("TAECEL_NIP", self.config.nip),
            )
            if not value
        ]

        if missing:
            return ProviderHealth(
                status=ProviderStatus.PENDING_CONTRACT,
                detail=(
                    "Taecel requiere contrato comercial y levantamiento tecnologico. "
                    "Su documentacion de API no es publica: se entrega tras el alta. "
                    "Ver docs/api-integrations.md."
                ),
                missing_requirements=PENDING_REQUIREMENTS,
            )

        # Con credenciales presentes tampoco se declara READY: falta implementar
        # los metodos contra su documentacion real. Declararlo listo aqui seria
        # exactamente el tipo de afirmacion sin respaldo que este proyecto evita.
        return ProviderHealth(
            status=ProviderStatus.PENDING_CONTRACT,
            detail=(
                "Hay credenciales de Taecel, pero el adaptador aun no implementa "
                "sus endpoints. Falta la documentacion oficial del web service."
            ),
            missing_requirements=(
                "Documentacion oficial del web service de Taecel",
                "Implementacion de fetch_catalog, send_topup y get_topup_status",
            ),
        )

    def _not_ready(self) -> ProviderNotConfigured:
        return ProviderNotConfigured(
            provider=self.slug,
            message=(
                "La integracion con Taecel no esta activa. Requiere contrato "
                "comercial y su documentacion tecnica, que no es publica."
            ),
            missing_requirements=PENDING_REQUIREMENTS,
            status=ProviderStatus.PENDING_CONTRACT,
        )

    def fetch_catalog(self) -> list[CatalogProduct]:
        raise self._not_ready()

    def send_topup(self, request: TopupRequest) -> TopupResult:
        raise self._not_ready()

    def get_topup_status(self, provider_reference: str) -> TopupResult:
        raise self._not_ready()
