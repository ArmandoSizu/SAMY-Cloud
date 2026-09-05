"""Excepciones de proveedores.

La jerarquia distingue tres familias que la capa de aplicacion trata distinto:

* ``ProviderConfigurationError`` -> problema nuestro: falta configuracion o
  contrato. Nunca se reintenta. Se muestra al administrador, no al cajero.
* ``ProviderPermanentError`` -> el proveedor rechazo la operacion de forma
  definitiva (numero invalido, referencia inexistente, saldo insuficiente).
  No se reintenta. Si ya se cobro, dispara reembolso.
* ``ProviderTransientError`` -> fallo temporal (timeout, 502, rate limit).
  SI se reintenta con backoff. El resultado real queda indeterminado hasta
  reconciliar, por lo que la operacion queda en estado de revision, nunca en
  fallo silencioso.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from samy_common.providers.base import ProviderStatus

__all__ = [
    "ProviderError",
    "ProviderConfigurationError",
    "ProviderNotConfigured",
    "ProviderNotFound",
    "ProviderCapabilityError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "ProviderIndeterminateError",
]


class ProviderError(Exception):
    """Base de todos los errores relacionados con proveedores externos."""

    #: Si la operacion puede reintentarse de forma segura.
    retryable: bool = False
    #: Codigo estable para logs, metricas y traduccion en UI.
    code: str = "provider_error"

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        external_code: str | None = None,
        external_reference: str | None = None,
    ) -> None:
        self.provider = provider
        self.message = message
        self.external_code = external_code
        self.external_reference = external_reference
        super().__init__(f"[{provider}] {message}")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "provider": self.provider,
            "message": self.message,
            "retryable": self.retryable,
            "external_code": self.external_code,
            "external_reference": self.external_reference,
        }


# --- Configuracion (culpa nuestra, no del proveedor) ---------------------


class ProviderConfigurationError(ProviderError):
    code = "provider_configuration_error"


class ProviderNotConfigured(ProviderConfigurationError):
    """El adaptador existe pero no tiene credenciales validas.

    Esta es la excepcion que impide que SAMY Cloud invente resultados.
    """

    code = "provider_not_configured"

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        missing_requirements: tuple[str, ...] = (),
        status: "ProviderStatus | None" = None,
    ) -> None:
        self.missing_requirements = missing_requirements
        self.status = status
        super().__init__(provider, message)

    def to_dict(self) -> dict[str, object]:
        data = super().to_dict()
        data["missing_requirements"] = list(self.missing_requirements)
        data["status"] = str(self.status) if self.status else None
        return data


class ProviderNotFound(ProviderConfigurationError):
    code = "provider_not_found"


class ProviderCapabilityError(ProviderConfigurationError):
    code = "provider_capability_error"


# --- Errores de negocio del proveedor ------------------------------------


class ProviderPermanentError(ProviderError):
    """El proveedor rechazo definitivamente. Reintentar no cambiara nada."""

    code = "provider_permanent_error"
    retryable = False


class ProviderTransientError(ProviderError):
    """Fallo temporal. Reintentable con backoff exponencial."""

    code = "provider_transient_error"
    retryable = True


class ProviderIndeterminateError(ProviderError):
    """No sabemos si la operacion se ejecuto (timeout tras enviar la peticion).

    Este es el caso mas peligroso en un sistema de dinero. NUNCA se asume
    fallo ni exito: la operacion se marca para reconciliacion y se consulta el
    estado real al proveedor por su ID de idempotencia antes de decidir.
    """

    code = "provider_indeterminate"
    retryable = False
