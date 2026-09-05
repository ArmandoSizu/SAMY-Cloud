"""Contrato de los proveedores de recargas.

Toda integracion de tiempo aire implementa ``TopupProvider``. El microservicio
no conoce a Reloadly ni a Taecel: conoce este contrato.

Regla que gobierna todos los adaptadores: sin credenciales, ``ensure_ready()``
levanta ``ProviderNotConfigured`` y la recarga NO se intenta. Nunca se devuelve
un ``TopupResult`` exitoso sin confirmacion real del proveedor.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from typing import Any

from samy_common.money import Money
from samy_common.providers.base import BaseProvider

__all__ = [
    "TopupProvider",
    "TopupRequest",
    "TopupResult",
    "TopupStatus",
    "CatalogProduct",
]


class TopupStatus:
    """Resultado de una solicitud de recarga."""

    #: El operador confirmo la entrega. Unico valor que cierra la venta.
    SUCCEEDED = "SUCCEEDED"
    #: Aceptada pero aun en proceso en la red del operador.
    PENDING = "PENDING"
    #: Rechazada de forma definitiva.
    FAILED = "FAILED"
    #: Desconocido. Requiere consulta de estado, jamas suposicion.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    """Un producto vendible, tal como lo publica el proveedor.

    Se construye SOLO a partir de la respuesta real del proveedor. Si el
    proveedor no ofrece una denominacion, no existe aqui y por tanto no puede
    mostrarse ni venderse.
    """

    provider_slug: str
    provider_product_id: str
    operator_code: str
    operator_name: str
    label: str
    #: Monto fijo. ``None`` cuando el producto acepta monto libre.
    amount: Money | None = None
    min_amount: Money | None = None
    max_amount: Money | None = None
    is_data_package: bool = False
    validity_days: int | None = None
    logo_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open_amount(self) -> bool:
        return self.amount is None


@dataclass(frozen=True, slots=True)
class TopupRequest:
    """Solicitud de recarga. Solo se construye con la orden ya PAGADA."""

    fulfillment_id: uuid.UUID
    order_id: uuid.UUID
    operator_code: str
    product_id: str
    amount: Money
    #: Numero en formato internacional (+52...), para los proveedores que lo
    #: piden asi.
    phone_e164: str
    #: Numero nacional de 10 digitos, sin codigo de pais. Reloadly lo espera
    #: en esta forma junto con ``countryCode`` por separado.
    phone_national: str
    #: Numero enmascarado, unico apto para logs y comprobantes.
    phone_masked: str
    #: NUESTRA referencia para conciliar esta recarga con el proveedor.
    #:
    #: Se llama "idempotency_key" por historia, pero cuidado: que el proveedor
    #: la respete como clave de idempotencia depende del proveedor y hay que
    #: comprobarlo en su documentacion. Reloadly NO lo garantiza.
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class TopupResult:
    status: str
    provider_reference: str
    provider_mode: str
    operator_reference: str = ""
    delivered_amount: Money | None = None
    failure_reason: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == TopupStatus.SUCCEEDED


class TopupProvider(BaseProvider[Any], abc.ABC):
    """Contrato de un proveedor de tiempo aire."""

    @abc.abstractmethod
    def fetch_catalog(self) -> list[CatalogProduct]:
        """Descarga el catalogo real de operadores y denominaciones."""
        raise NotImplementedError

    @abc.abstractmethod
    def send_topup(self, request: TopupRequest) -> TopupResult:
        """Envia la recarga.

        **No se asume que el proveedor deduplique.** Si el envio termina en
        timeout, el adaptador debe levantar ``ProviderIndeterminateError`` y
        NUNCA reintentar por su cuenta: quien reintenta a ciegas una recarga
        que quiza ya se aplico, la aplica dos veces y paga las dos.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_topup_status(self, provider_reference: str) -> TopupResult:
        """Consulta el estado real. Es el mecanismo de conciliacion."""
        raise NotImplementedError

    def find_by_custom_identifier(self, custom_identifier: str) -> TopupResult | None:
        """Busca una recarga por NUESTRA referencia, sin conocer la del proveedor.

        Es lo que permite resolver un envio indeterminado: tras un timeout no
        tenemos ``provider_reference``, solo nuestra propia clave, y hay que
        averiguar si la recarga existe antes de reintentar o reembolsar.

        Por omision devuelve ``None`` ("no lo se"), que es la respuesta segura:
        un adaptador que no sepa buscar deja la recarga en revision manual en
        vez de arriesgar un duplicado.
        """
        return None
