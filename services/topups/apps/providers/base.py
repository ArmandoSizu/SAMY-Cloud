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
from datetime import datetime
from typing import Any

from samy_common.money import Money
from samy_common.providers.base import BaseProvider
from samy_common.saldo import SaldoProveedor

__all__ = [
    "TopupProvider",
    "TopupRequest",
    "TopupResult",
    "TopupStatus",
    "CatalogProduct",
    "ContextoConciliacion",
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

    #: ``True`` cuando el importe ya viene dentro de ``product_id`` (el SKU es
    #: "Amigo Sin Limite $100") y por tanto NO debe mandarse por separado.
    #:
    #: No es un detalle de estilo. Hay proveedores que, al recibir SKU de
    #: denominacion fija y monto a la vez, ignoran uno de los dos en silencio:
    #: se cobra una cosa y se entrega otra. Quien sabe la respuesta es el
    #: mapping verificado del catalogo, no el adaptador, y por eso viaja aqui
    #: en vez de adivinarse en cada proveedor.
    #:
    #: Por omision ``False`` (mandar el monto) porque es lo que espera
    #: Reloadly, el unico proveedor implementado hoy.
    amount_in_sku: bool = False


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


@dataclass(frozen=True, slots=True)
class ContextoConciliacion:
    """Todo lo que se sabe de una recarga que quedo sin desenlace.

    POR QUE EXISTE
    --------------

    Los dos ganchos historicos de conciliacion suponen que el proveedor sabe
    contestar a una de estas dos preguntas:

        get_topup_status(folio_del_proveedor)
        find_by_custom_identifier(clave_nuestra)

    Hay proveedores que no saben contestar a ninguna. Linntae es uno: sus dos
    consultas identifican una recarga por ``idOffer`` + ``telefono``, no por
    folio ni por ninguna referencia nuestra. Con solo esos dos ganchos, la
    unica implementacion honesta para un proveedor asi es devolver "no lo se"
    siempre, y entonces ninguna recarga suya se concilia nunca.

    Este contexto lleva los datos de la OPERACION, no una clave. Con eso se
    puede preguntar a cualquier proveedor en los terminos que acepte.

    Es opcional por diseno: ``estado_por_contexto()`` devuelve ``None`` por
    omision, asi que los adaptadores que ya funcionan por folio no cambian de
    comportamiento.
    """

    fulfillment_id: uuid.UUID
    #: Identificador del producto EN EL PROVEEDOR. Para Linntae, el idOffer.
    provider_product_id: str
    #: Numero nacional de 10 digitos.
    phone_national: str
    amount: Money
    #: Nuestra clave. Sirve para registrar, no necesariamente para preguntar.
    idempotency_key: str
    #: Folio del proveedor si alcanzo a llegar. Vacio tras un timeout.
    provider_reference: str = ""
    #: Cuando se envio. Decide que consultas tienen sentido: hay proveedores
    #: con ventanas de tiempo (Linntae: 60 segundos para la consulta directa).
    enviado_en: datetime | None = None


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

    def saldo_disponible(self) -> "SaldoProveedor":
        """Saldo que el proveedor dice tener. Se usa ANTES de cobrar.

        Por omision devuelve ``SALDO_NO_REPORTADO``, que es la respuesta
        segura: "no lo se". Un adaptador que no sepa consultar su saldo no
        puede afirmar que hay fondos, y en produccion eso bloquea la venta
        (ver ``samy_common.saldo``). Devolver cero por omision seria peor en
        las dos direcciones: bloquearia proveedores que si tienen fondos, y
        confundiria "no lo dijo" con "dijo que no tiene".

        Se consulta una vez y se cachea unos segundos: Reloadly castiga el
        exceso de llamadas suspendiendo la cuenta, y reactivarla exige hablar
        con soporte. Ver ``apps.fulfillment.saldo``.
        """
        from samy_common.saldo import SALDO_NO_REPORTADO

        return SALDO_NO_REPORTADO

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

    def estado_por_contexto(
        self, contexto: "ContextoConciliacion"
    ) -> TopupResult | None:
        """Consulta el estado con los DATOS de la operacion, no con un folio.

        Es el gancho para proveedores cuya API no acepta una consulta por
        folio ni por referencia propia. Ver ``ContextoConciliacion``.

        La conciliacion lo intenta ANTES de los otros dos, porque un
        adaptador que implementa esto lo hace justamente porque es su unica
        consulta fiable. Por omision devuelve ``None``, asi que los
        adaptadores que conciliaban por folio siguen haciendolo igual.

        ``None`` significa "no lo se" y deja la recarga en revision. Nunca
        debe devolverse un resultado FAILED por no haber encontrado registro:
        la ausencia de evidencia no es evidencia de ausencia, y aqui la
        diferencia es reembolsar a un cliente que si recibio su saldo.
        """
        return None
