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


def _build(service: str, read_timeout: float | None = None) -> ServiceClient:
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
            read_timeout=read_timeout or settings.SERVICE_TIMEOUT_READ,
        )
    )


# Un cliente por servicio, reutilizado entre peticiones para aprovechar el
# pool de conexiones de httpx. Abrir una conexion TCP por peticion HTTP
# interna anade decenas de milisegundos innecesarios.
@lru_cache(maxsize=None)
def payments_client() -> ServiceClient:
    return _build("payments")


@lru_cache(maxsize=None)
def payments_client_cobro() -> ServiceClient:
    """Cliente para las llamadas que MUEVEN dinero contra una pasarela.

    Existe por un problema de orden en los limites de tiempo. El cliente
    normal espera 15 s; payments espera hasta 20 s a Conekta. Es decir, el
    BFF se rendia ANTES que el servicio al que llamo, en la ventana de 15 a
    20 segundos: el cajero veia un error mientras el cobro seguia en curso y
    podia acabar cobrado. El que espera arriba tiene que aguantar mas que el
    que espera abajo, nunca menos.

    30 s cubre el peor caso de Conekta (20 s) con margen para la red interna.
    """
    return _build("payments", read_timeout=30.0)


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
