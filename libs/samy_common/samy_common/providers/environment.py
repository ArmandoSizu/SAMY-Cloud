"""Separacion de ambientes: sandbox y produccion no se mezclan. Nunca.

EL FALLO QUE ESTE MODULO EVITA
------------------------------

Hay dos, y son simetricos. Los dos han arruinado lanzamientos reales:

1. **Produccion vendiendo contra un sandbox.** El cajero cobra $100 de
   verdad, el proveedor de pruebas responde "exito", y el cliente se va sin
   recarga. El dinero entro y el servicio no salio. Es el peor fallo posible
   en este sistema porque parece que funciono.

2. **Un ambiente de pruebas pegado a produccion.** Alguien corre la suite con
   credenciales productivas y cada prueba gasta saldo real y manda recargas a
   telefonos reales. Silencioso, caro e irreversible.

LA REGLA
--------

    El ambiente del servicio y el modo del proveedor tienen que concordar.

        ENVIRONMENT=production   <->  ProviderMode.PRODUCTION
        cualquier otro ambiente  <->  ProviderMode.SANDBOX

No hay combinacion permitida fuera de esas dos. Ni "produccion con sandbox
temporalmente para probar", ni "desarrollo apuntando a produccion un ratito".
Esas dos frases son precisamente como se llega a los dos fallos de arriba.

FAIL-CLOSED
-----------

Un valor de ``ENVIRONMENT`` que no se reconoce **no** se interpreta como
desarrollo: se rechaza. Un servicio que no sabe en que ambiente esta no puede
decidir si le corresponde mover dinero real, y la respuesta segura a "no lo
se" es negarse.

Donde se aplica: ``BaseProvider.ensure_ready()``, que es la guardia por la que
pasa toda operacion que mueve dinero. Ponerlo ahi y no en cada adaptador
significa que un proveedor nuevo hereda la proteccion sin acordarse de ella.
"""

from __future__ import annotations

import enum
from typing import Final

from samy_common.providers.base import ProviderMode
from samy_common.providers.exceptions import ProviderNotConfigured

__all__ = [
    "RuntimeEnvironment",
    "ProviderEnvironmentMismatch",
    "resolver_ambiente",
    "ambiente_actual",
    "modo_esperado",
    "verificar_ambiente",
]


class RuntimeEnvironment(enum.StrEnum):
    """Ambiente en el que corre el servicio."""

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


#: Sinonimos que se aceptan al leer la variable de entorno. Deliberadamente
#: corta: aceptar cualquier cosa parecida a "prod" es como acaba un servidor
#: de pruebas creyendose productivo.
_SINONIMOS: Final[dict[str, RuntimeEnvironment]] = {
    "development": RuntimeEnvironment.DEVELOPMENT,
    "dev": RuntimeEnvironment.DEVELOPMENT,
    "local": RuntimeEnvironment.DEVELOPMENT,
    "test": RuntimeEnvironment.TEST,
    "testing": RuntimeEnvironment.TEST,
    "ci": RuntimeEnvironment.TEST,
    "staging": RuntimeEnvironment.STAGING,
    "stage": RuntimeEnvironment.STAGING,
    "production": RuntimeEnvironment.PRODUCTION,
    "prod": RuntimeEnvironment.PRODUCTION,
}


class ProviderEnvironmentMismatch(ProviderNotConfigured):
    """El modo del proveedor no corresponde al ambiente del servicio.

    Hereda de ``ProviderNotConfigured`` a proposito, y no es por comodidad:
    toda la aplicacion ya sabe traducir esa excepcion a "integracion pendiente"
    en la UI y a un registro de auditoria. Una clase hermana nueva se
    convertiria en un error 500 en la primera pantalla que no la contemple, y
    un 500 no le dice al cajero que no puede vender: le dice que la app se
    rompio. El ``code`` propio permite distinguirlo en metricas.
    """

    code = "provider_environment_mismatch"


def resolver_ambiente(raw: str) -> RuntimeEnvironment:
    """Traduce ``ENVIRONMENT`` a un ambiente. Rechaza lo que no reconoce.

    No hay valor por omision. Un ``ENVIRONMENT`` vacio o desconocido es un
    error de configuracion, no un caso a interpretar: adivinar aqui es
    adivinar si toca mover dinero real.
    """
    clave = (raw or "").strip().lower()
    if not clave:
        raise ProviderEnvironmentMismatch(
            provider="samy",
            message=(
                "ENVIRONMENT no esta definido. El servicio no puede saber si le "
                "corresponde operar contra sandbox o contra produccion, y por "
                "eso no opera. Valores validos: "
                + ", ".join(sorted(set(_SINONIMOS)))
            ),
        )
    try:
        return _SINONIMOS[clave]
    except KeyError as exc:
        raise ProviderEnvironmentMismatch(
            provider="samy",
            message=(
                f"ENVIRONMENT='{raw}' no se reconoce. No se asume desarrollo: un "
                "ambiente desconocido no opera. Valores validos: "
                + ", ".join(sorted(set(_SINONIMOS)))
            ),
        ) from exc


def ambiente_actual() -> RuntimeEnvironment:
    """Ambiente del servicio, leido de ``settings.ENVIRONMENT``.

    El import de Django va dentro a proposito: ``samy_common.money`` se usa en
    contextos sin Django configurado y no tiene por que arrastrarlo.
    """
    from django.conf import settings

    return resolver_ambiente(str(getattr(settings, "ENVIRONMENT", "") or ""))


def modo_esperado(ambiente: RuntimeEnvironment) -> ProviderMode:
    """El unico modo de proveedor admisible en ese ambiente."""
    if ambiente is RuntimeEnvironment.PRODUCTION:
        return ProviderMode.PRODUCTION
    return ProviderMode.SANDBOX


def verificar_ambiente(
    *,
    provider_slug: str,
    provider_mode: ProviderMode,
    ambiente: RuntimeEnvironment | None = None,
) -> None:
    """Levanta si el modo del proveedor no corresponde al ambiente.

    Los dos mensajes son distintos a proposito: son dos errores distintos y
    quien los lea a las once de la noche necesita saber cual de los dos tiene.
    """
    if ambiente is None:
        ambiente = ambiente_actual()

    esperado = modo_esperado(ambiente)
    if provider_mode is esperado:
        return

    if ambiente is RuntimeEnvironment.PRODUCTION:
        raise ProviderEnvironmentMismatch(
            provider=provider_slug,
            message=(
                f"El servicio corre en produccion pero '{provider_slug}' esta en "
                f"modo {provider_mode}. Se detiene: cobrar dinero real y "
                "entregarlo contra un sandbox deja al cliente sin servicio y el "
                "cobro hecho. Pon las credenciales de produccion del proveedor o "
                "deja de vender ese producto."
            ),
            missing_requirements=(
                f"Credenciales de PRODUCCION de {provider_slug}",
            ),
        )

    raise ProviderEnvironmentMismatch(
        provider=provider_slug,
        message=(
            f"'{provider_slug}' esta en modo PRODUCTION pero el servicio corre "
            f"en ambiente '{ambiente}'. Se detiene: operar asi gasta saldo real "
            "y manda recargas a telefonos reales desde un entorno que no es "
            "produccion. Usa las credenciales de sandbox del proveedor."
        ),
        missing_requirements=(f"Credenciales de SANDBOX de {provider_slug}",),
    )
