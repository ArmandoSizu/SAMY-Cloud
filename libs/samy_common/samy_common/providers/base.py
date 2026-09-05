"""Contrato base para todos los proveedores externos de SAMY Cloud.

Este modulo implementa la regla mas importante del proyecto:

    **Un proveedor sin credenciales NUNCA devuelve un resultado exitoso.**

En lugar de simular una respuesta, un adaptador no configurado levanta
``ProviderNotConfigured``. La capa de aplicacion traduce esa excepcion a un
estado visible en la UI ("Integracion pendiente de credenciales") y a un
registro de auditoria. En ningun punto del sistema existe un camino que
produzca una transaccion falsa.

Cada adaptador declara ademas su ``mode`` (SANDBOX o PRODUCTION). El modo se
propaga a la orden, al comprobante y a los logs, de forma que jamas se puede
confundir una operacion de pruebas con una real.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

__all__ = [
    "ProviderMode",
    "ProviderStatus",
    "ProviderCapability",
    "ProviderHealth",
    "BaseProvider",
    "ProviderRegistry",
]


class ProviderMode(enum.StrEnum):
    """Ambiente contra el que apunta el adaptador.

    ``SANDBOX`` y ``PRODUCTION`` jamas se mezclan: son credenciales distintas,
    URLs distintas y bases de datos logicas distintas. El modo queda grabado en
    cada transaccion.
    """

    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


class ProviderStatus(enum.StrEnum):
    """Estado operativo de una integracion.

    Solo ``READY`` permite ejecutar operaciones reales.
    """

    #: Falta configuracion (API key, secreto, contrato). No opera.
    NOT_CONFIGURED = "NOT_CONFIGURED"
    #: Configurado, pero el contrato comercial con el proveedor no esta firmado.
    PENDING_CONTRACT = "PENDING_CONTRACT"
    #: Configurado y verificado. Puede operar.
    READY = "READY"
    #: Configurado pero el proveedor esta caido o rechazando peticiones.
    DEGRADED = "DEGRADED"
    #: Deshabilitado manualmente por un administrador de la plataforma.
    DISABLED = "DISABLED"


class ProviderCapability(enum.StrEnum):
    """Capacidades que un adaptador puede declarar."""

    # Pagos
    CARD_PAYMENT = "CARD_PAYMENT"
    CASH_PAYMENT = "CASH_PAYMENT"
    BANK_TRANSFER = "BANK_TRANSFER"
    DYNAMIC_QR = "DYNAMIC_QR"
    REFUND = "REFUND"
    WEBHOOK_SIGNED = "WEBHOOK_SIGNED"
    HOSTED_CHECKOUT = "HOSTED_CHECKOUT"
    TOKENIZATION = "TOKENIZATION"
    # Recargas
    AIRTIME_TOPUP = "AIRTIME_TOPUP"
    DATA_PACKAGE = "DATA_PACKAGE"
    CATALOG_SYNC = "CATALOG_SYNC"
    # Servicios
    BILL_INQUIRY = "BILL_INQUIRY"
    BILL_PAYMENT = "BILL_PAYMENT"


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Resultado de un chequeo de salud contra el proveedor."""

    status: ProviderStatus
    detail: str
    #: Que falta exactamente para poder operar. Se muestra en el panel admin.
    missing_requirements: tuple[str, ...] = field(default_factory=tuple)
    latency_ms: int | None = None

    @property
    def is_operational(self) -> bool:
        return self.status is ProviderStatus.READY


ConfigT = TypeVar("ConfigT")


class BaseProvider(abc.ABC, Generic[ConfigT]):
    """Clase base de todo adaptador de proveedor externo.

    Subclases obligatorias: ``slug``, ``display_name``, ``capabilities``,
    ``required_settings`` y ``check_health()``.
    """

    #: Identificador estable usado en base de datos y configuracion.
    slug: ClassVar[str]
    #: Nombre legible mostrado en la UI.
    display_name: ClassVar[str]
    #: Capacidades reales del adaptador. No declares lo que no implementaste.
    capabilities: ClassVar[frozenset[ProviderCapability]] = frozenset()
    #: Nombres de variables de entorno necesarias para operar.
    required_settings: ClassVar[tuple[str, ...]] = ()
    #: Si es True, el proveedor exige contrato comercial firmado ademas de llaves.
    requires_commercial_contract: ClassVar[bool] = False
    #: Documentacion oficial, para el panel de administracion.
    documentation_url: ClassVar[str | None] = None

    def __init__(self, config: ConfigT, mode: ProviderMode) -> None:
        self.config = config
        self.mode = mode

    # -- introspeccion --------------------------------------------------

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities

    def require(self, capability: ProviderCapability) -> None:
        """Levanta si el adaptador no soporta la capacidad solicitada."""
        if not self.supports(capability):
            raise ProviderCapabilityError(
                provider=self.slug,
                message=(
                    f"El proveedor '{self.display_name}' no soporta "
                    f"'{capability}'. Capacidades declaradas: "
                    f"{sorted(self.capabilities) or 'ninguna'}."
                ),
            )

    @abc.abstractmethod
    def check_health(self) -> ProviderHealth:
        """Verifica configuracion y conectividad reales contra el proveedor.

        Debe hacer una llamada real (o al menos validar credenciales) y NUNCA
        devolver ``READY`` sin haberlo comprobado.
        """
        raise NotImplementedError

    def ensure_ready(self) -> None:
        """Guardia previa a cualquier operacion que mueva dinero."""
        health = self.check_health()
        if not health.is_operational:
            raise ProviderNotConfigured(
                provider=self.slug,
                message=health.detail,
                missing_requirements=health.missing_requirements,
                status=health.status,
            )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} slug={self.slug!r} mode={self.mode}>"


class ProviderRegistry:
    """Registro de adaptadores disponibles en un servicio.

    Permite agregar proveedores nuevos sin tocar la logica de negocio: el
    servicio pide un adaptador por slug y el registro lo construye.
    """

    def __init__(self) -> None:
        self._providers: dict[str, type[BaseProvider[Any]]] = {}

    def register(self, provider_cls: type[BaseProvider[Any]]) -> type[BaseProvider[Any]]:
        """Decorador de registro. Falla ruidosamente ante slugs duplicados."""
        slug = getattr(provider_cls, "slug", None)
        if not slug:
            raise ValueError(f"{provider_cls.__name__} no define 'slug'.")
        if slug in self._providers:
            raise ValueError(f"Proveedor duplicado para slug '{slug}'.")
        self._providers[slug] = provider_cls
        return provider_cls

    def get(self, slug: str) -> type[BaseProvider[Any]]:
        try:
            return self._providers[slug]
        except KeyError as exc:
            raise ProviderNotFound(
                provider=slug,
                message=(
                    f"No existe adaptador registrado con slug '{slug}'. "
                    f"Registrados: {sorted(self._providers) or 'ninguno'}."
                ),
            ) from exc

    def all_slugs(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def describe_all(self) -> list[dict[str, Any]]:
        """Metadatos de todos los adaptadores, para el panel de administracion."""
        return [
            {
                "slug": cls.slug,
                "display_name": cls.display_name,
                "capabilities": sorted(cls.capabilities),
                "required_settings": list(cls.required_settings),
                "requires_commercial_contract": cls.requires_commercial_contract,
                "documentation_url": cls.documentation_url,
            }
            for cls in sorted(self._providers.values(), key=lambda c: c.slug)
        ]


# Importacion tardia para evitar ciclo: exceptions importa ProviderStatus.
from samy_common.providers.exceptions import (  # noqa: E402
    ProviderCapabilityError,
    ProviderNotConfigured,
    ProviderNotFound,
)
