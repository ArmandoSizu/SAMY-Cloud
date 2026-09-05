"""Clientes hacia los microservicios comerciales.

El Core Platform actua como **Backend For Frontend**: el navegador nunca habla
directamente con los microservicios. Esto es deliberado.

Ventajas frente a exponer los servicios al navegador:

* No hay tokens de servicio en el navegador. El unico credencial del lado del
  cliente es la cookie de sesion, con HttpOnly.
* No hay CORS: todo sale del mismo origen.
* La autorizacion multi-tenant se resuelve una vez, en el Core, y los
  servicios reciben ya el ``store_id`` verificado y firmado.
* La superficie expuesta a internet es una sola.

Los microservicios solo son accesibles dentro de la red interna y solo
aceptan peticiones firmadas con HMAC.
"""

from __future__ import annotations

from functools import lru_cache

from django.conf import settings

from samy_common.http.client import ServiceClient, ServiceClientConfig


def _build(service: str) -> ServiceClient:
    try:
        base_url = settings.SERVICE_URLS[service]
    except KeyError as exc:  # pragma: no cover - error de configuracion
        raise RuntimeError(
            f"No hay URL configurada para el servicio '{service}'. "
            f"Configurados: {sorted(settings.SERVICE_URLS)}."
        ) from exc

    return ServiceClient(
        ServiceClientConfig(
            base_url=base_url,
            secret=settings.SERVICE_S2S_SECRET,
            caller=settings.SERVICE_NAME,
            connect_timeout=settings.SERVICE_TIMEOUT_CONNECT,
            read_timeout=settings.SERVICE_TIMEOUT_READ,
        )
    )


# Un cliente por servicio, reutilizado entre peticiones para aprovechar el
# pool de conexiones de httpx. Abrir una conexion TCP por peticion HTTP
# interna anade decenas de milisegundos innecesarios.
@lru_cache(maxsize=None)
def payments_client() -> ServiceClient:
    return _build("payments")


@lru_cache(maxsize=None)
def topups_client() -> ServiceClient:
    return _build("topups")


@lru_cache(maxsize=None)
def billpay_client() -> ServiceClient:
    return _build("billpay")


def all_clients() -> dict[str, ServiceClient]:
    """Todos los clientes, para chequeos de salud agregados."""
    return {
        "payments": payments_client(),
        "topups": topups_client(),
        "billpay": billpay_client(),
    }
