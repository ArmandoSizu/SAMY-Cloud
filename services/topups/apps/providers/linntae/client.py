"""Transporte HTTP hacia Linntae. Nada de logica de negocio.

Aqui viven tres cosas y ninguna es opcional en un sistema de dinero:

1. **Los tiempos de espera.** Explicitos y separados (conectar, leer). Sin
   ellos, un proveedor lento no da un error: se queda con el worker, y el
   siguiente cliente ve la caja colgada.

2. **La politica de reintentos, escrita como DOS METODOS DISTINTOS.** Las
   lecturas se reintentan; una compra, nunca. No es un ``if`` dentro de un
   metodo comun: son ``leer()`` y ``comprar()``, y ``comprar()`` no tiene
   bucle. Un ``if`` se puede invertir al refactorizar; un bucle que no existe,
   no.

3. **La guarda de host.** ``LINNTAE_ENV=demo`` solo puede hablar con
   ``apidemo.linn.mx``, y ``production`` solo con ``api.linn.mx``. Poner la
   URL de produccion con la etiqueta de demo es la forma mas facil de mandar
   una recarga real creyendo que es de prueba, y aqui es imposible: el
   adaptador no arranca.

LO QUE NUNCA SALE DE AQUI
------------------------

Ni la contrasena, ni el token, ni el numero completo del cliente. El registro
de cada peticion lleva la ruta, el estado HTTP, el ``code`` de Linntae y las
LLAVES del cuerpo -no sus valores-. El telefono se registra enmascarado.

``verify`` no se toca. No existe una bandera para desactivar la verificacion
de certificados: es la clase de opcion que alguien enciende "un momento para
probar" y se queda encendida.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Final, Mapping
from urllib.parse import urlparse

import httpx
import structlog

from apps.providers.linntae.auth import (
    TTL_POR_OMISION,
    Autenticador,
    CredencialesLinntaeInvalidas,
    TokenLinntae,
)
from apps.providers.linntae.codigos import (
    CODIGO_EXITO,
    Consecuencia,
    Endpoint,
    consecuencia_de_compra,
    consecuencia_de_consulta,
    consecuencia_http,
    es_geobloqueo,
    significado_code_4,
)
from samy_common.providers.exceptions import (
    ProviderIndeterminateError,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.security.masking import mask_phone

__all__ = [
    "LinntaeConfig",
    "LinntaeClient",
    "RespuestaLinntae",
    "AMBIENTE_DEMO",
    "AMBIENTE_PRODUCCION",
    "HOST_POR_AMBIENTE",
    "URL_DEMO",
    "URL_PRODUCCION",
]

log = structlog.get_logger("provider.linntae.client")

AMBIENTE_DEMO: Final[str] = "demo"
AMBIENTE_PRODUCCION: Final[str] = "production"

#: Las dos URLs que publica la especificacion OpenAPI de Linntae (v2.0.0).
URL_DEMO: Final[str] = "https://apidemo.linn.mx/api/v1/"
URL_PRODUCCION: Final[str] = "https://api.linn.mx/api/v1/"

#: El unico host legitimo de cada ambiente. La guarda que impide confundirlos.
HOST_POR_AMBIENTE: Final[dict[str, str]] = {
    AMBIENTE_DEMO: "apidemo.linn.mx",
    AMBIENTE_PRODUCCION: "api.linn.mx",
}

#: Margen con el que se renueva el token ANTES de una compra.
#:
#: Un 401 en mitad de una compra deja el resultado indeterminado, y resolverlo
#: cuesta una conciliacion manual. Renovar el token si le queda menos de este
#: margen convierte un caso ambiguo raro en una llamada de mas barata.
MARGEN_TOKEN_COMPRA_SEGUNDOS: Final[int] = 60


@dataclass(frozen=True, slots=True)
class LinntaeConfig:
    """Configuracion de Linntae. Todo llega por entorno; nada vive en el repo.

    Sin valores por omision en credenciales ni en URL: un valor inventado
    aqui seria una peticion a un servidor que no es Linntae, o a la cuenta de
    alguien mas.
    """

    base_url: str = ""
    username: str = ""
    password: str = ""
    #: ``demo`` o ``production``. Cualquier otra cosa no opera.
    ambiente: str = AMBIENTE_DEMO

    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    token_ttl_segundos: int = TTL_POR_OMISION
    #: Reintentos de LECTURA. Las compras no se reintentan nunca.
    reintentos_lectura: int = 2

    #: ``typeBalance`` que Linntae espera en una compra. **Sin valor por
    #: omision a proposito.** La especificacion lo declara como entero
    #: obligatorio pero no publica su enumeracion; que ``1`` sea "SALDO
    #: PLATAFORMA" es una inferencia de sus ejemplos, no un dato. Mandar el
    #: numero equivocado gastaria la bolsa equivocada.
    type_balance: int | None = None

    #: ``extraComision``: lo que Linntae agrega al cobro del cliente en SU
    #: punto de venta. En SAMY el cobro al cliente lo hace SAMY, con su propio
    #: motor de precios, asi que aqui va CERO. Ver docs/providers/linntae.md.
    extra_comision: int = 0

    #: Interruptor final para cualquier operacion que mueva dinero real.
    permitir_operaciones_reales: bool = False

    @property
    def credenciales_completas(self) -> bool:
        return bool(self.base_url and self.username and self.password)

    @property
    def host(self) -> str:
        return (urlparse(self.base_url).hostname or "").lower()

    @property
    def ambiente_normalizado(self) -> str:
        return (self.ambiente or "").strip().lower()

    def problemas_de_ambiente(self) -> tuple[str, ...]:
        """Todo lo que impide confiar en la pareja (ambiente, URL).

        Se devuelven TODOS los problemas y no el primero: quien esta
        configurando esto quiere arreglarlo de una vez, no descubrir el
        siguiente en cada reinicio.
        """
        fallos: list[str] = []
        ambiente = self.ambiente_normalizado

        if ambiente not in HOST_POR_AMBIENTE:
            fallos.append(
                f"LINNTAE_ENV='{self.ambiente}' no se reconoce. Los unicos "
                f"valores validos son: {', '.join(sorted(HOST_POR_AMBIENTE))}. "
                "No se asume ninguno: un ambiente adivinado decide si la "
                "recarga es de prueba o real."
            )

        if not self.base_url:
            fallos.append("LINNTAE_BASE_URL no esta definida.")
            return tuple(fallos)

        partes = urlparse(self.base_url)
        if partes.scheme != "https":
            fallos.append(
                f"LINNTAE_BASE_URL usa '{partes.scheme or 'sin esquema'}'. "
                "Solo se habla con Linntae por HTTPS."
            )

        esperado = HOST_POR_AMBIENTE.get(ambiente)
        if esperado and self.host != esperado:
            fallos.append(
                f"LINNTAE_ENV='{ambiente}' exige el host '{esperado}' y "
                f"LINNTAE_BASE_URL apunta a '{self.host or 'sin host'}'. Se "
                "detiene: es la forma mas directa de mandar una recarga real "
                "creyendo que es de prueba."
            )

        return tuple(fallos)


@dataclass(frozen=True, slots=True)
class RespuestaLinntae:
    """Una respuesta de Linntae ya interpretada.

    Lleva las tres capas separadas porque las tres importan y se confunden
    facil: el ``http_status`` dice si la conversacion funciono, el ``code``
    dice si la operacion funciono, y la ``consecuencia`` dice que le paso al
    dinero. Un HTTP 200 con ``code: 3`` es una conversacion perfecta y una
    venta que no ocurrio.
    """

    http_status: int
    code: int | None
    mensaje: str
    payload: dict[str, Any] = field(default_factory=dict)
    consecuencia: Consecuencia = Consecuencia.NO_EJECUTADA

    @property
    def ok(self) -> bool:
        return self.http_status == 200 and self.code == CODIGO_EXITO


class LinntaeClient:
    """Cliente HTTP de Linntae. Reutiliza conexion y no reintenta compras."""

    slug: Final[str] = "linntae"

    def __init__(self, config: LinntaeConfig) -> None:
        self.config = config
        self._cliente: httpx.Client | None = None
        self._auth = Autenticador(
            base_url=config.base_url,
            username=config.username,
            password=config.password,
            solicitar=self._solicitar_token,
            ttl_segundos=config.token_ttl_segundos,
            etiqueta_ambiente=config.ambiente_normalizado,
        )

    # -- ciclo de vida -----------------------------------------------------

    @property
    def auth(self) -> Autenticador:
        return self._auth

    def _httpx(self) -> httpx.Client:
        """Sesion reutilizable: una conexion TLS por proceso, no por venta."""
        if self._cliente is None or self._cliente.is_closed:
            self._cliente = httpx.Client(
                base_url=self.config.base_url,
                timeout=httpx.Timeout(
                    connect=self.config.connect_timeout,
                    read=self.config.read_timeout,
                    write=self.config.read_timeout,
                    pool=self.config.connect_timeout,
                ),
                # Sin redirecciones: un redirect de un POST de compra a otro
                # host es exactamente lo que no se quiere seguir a ciegas.
                follow_redirects=False,
                headers={"Accept": "application/json"},
            )
        return self._cliente

    def close(self) -> None:
        if self._cliente is not None and not self._cliente.is_closed:
            self._cliente.close()

    def __enter__(self) -> "LinntaeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- guardas -----------------------------------------------------------

    def exigir_ambiente_coherente(self) -> None:
        """Levanta si la pareja (ambiente, URL) no es una de las dos legitimas."""
        fallos = self.config.problemas_de_ambiente()
        if fallos:
            raise ProviderNotConfigured(
                provider=self.slug,
                message=" ".join(fallos),
                missing_requirements=tuple(fallos),
            )

    # -- token -------------------------------------------------------------

    def _solicitar_token(self, username: str, password: str) -> TokenLinntae:
        """``POST /getToken``. Es la unica peticion que no lleva Bearer.

        El cuerpo lleva la contrasena, asi que esta peticion **no registra su
        cuerpo ni sus llaves**, y el manejo de errores no incluye el cuerpo de
        la respuesta en el mensaje: Linntae hace eco del usuario en algunos
        mensajes de error.
        """
        self.exigir_ambiente_coherente()

        try:
            respuesta = self._httpx().post(
                str(Endpoint.TOKEN),
                json={"username": username, "password": password},
            )
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(
                provider=self.slug,
                message="Linntae no respondio a tiempo al pedir el token.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"No se pudo contactar a Linntae para autenticar: {type(exc).__name__}.",
            ) from exc

        payload = self._decodificar(respuesta)
        mensaje = str(payload.get("message") or "")
        pais = es_geobloqueo(mensaje)

        if pais:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    f"Linntae rechaza las peticiones desde el pais '{pais}' "
                    "(HTTP 403 country). No se reintenta: desde esta IP el "
                    "resultado sera el mismo. Hay que acordar con Linntae la "
                    "lista de paises o IPs permitidas antes de elegir region "
                    "de nube."
                ),
                external_code="PROVIDER_GEO_BLOCKED",
            )

        if respuesta.status_code in (401, 403):
            raise CredencialesLinntaeInvalidas(
                provider=self.slug,
                message=(
                    "Linntae rechazo las credenciales o la cuenta no esta "
                    "activa. Revisa LINNTAE_USERNAME y LINNTAE_PASSWORD en el "
                    ".env y el estado de la cuenta con su soporte."
                ),
                external_code=str(respuesta.status_code),
            )

        if respuesta.status_code >= 500:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Linntae devolvio {respuesta.status_code} al autenticar.",
                external_code=str(respuesta.status_code),
            )

        code = self._entero(payload.get("code"))
        if code == 4:
            raise CredencialesLinntaeInvalidas(
                provider=self.slug,
                message=significado_code_4(Endpoint.TOKEN, mensaje),
                external_code="4",
            )

        valor = str(payload.get("token") or "").strip()
        if code != CODIGO_EXITO or not valor:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    f"Linntae respondio code={code} sin token utilizable al "
                    "autenticar."
                ),
                external_code=str(code),
            )

        return TokenLinntae(
            valor=valor,
            support_id=self._entero(payload.get("supportId")),
            obtenido_en=time.time(),
        )

    # -- lectura -----------------------------------------------------------

    def leer(
        self,
        endpoint: Endpoint,
        *,
        cuerpo: Mapping[str, Any] | None = None,
        metodo: str = "GET",
    ) -> RespuestaLinntae:
        """Consulta de solo lectura. **Se reintenta** ante fallo transitorio.

        Reintentar aqui es seguro porque ninguna de estas rutas mueve dinero:
        saldo, catalogo, comisiones, esquema, historico de ventas. Un 500 en
        una lectura es un 500 y nada mas.

        El 401 se trata una sola vez: se invalida el token, se pide otro y se
        repite la peticion. Un segundo 401 se propaga. Ver el docstring de
        ``auth.py``: el bucle "401 -> token nuevo -> 401" es como se pierde el
        acceso a una cuenta.
        """
        self.exigir_ambiente_coherente()

        intentos = max(1, self.config.reintentos_lectura + 1)
        renovado = False
        ultimo: Exception | None = None

        for intento in range(1, intentos + 1):
            try:
                respuesta = self._enviar(endpoint, cuerpo=cuerpo, metodo=metodo)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                ultimo = exc
                if intento >= intentos:
                    raise ProviderTransientError(
                        provider=self.slug,
                        message=(
                            f"Linntae no respondio a {endpoint} "
                            f"({type(exc).__name__})."
                        ),
                    ) from exc
                time.sleep(min(0.5 * intento, 2.0))
                continue

            leida = self._interpretar(endpoint, respuesta, es_compra=False)

            if leida.consecuencia is Consecuencia.GEOBLOQUEO:
                raise ProviderPermanentError(
                    provider=self.slug,
                    message=(
                        "Linntae bloquea las peticiones desde este pais "
                        f"({leida.mensaje}). No se reintenta."
                    ),
                    external_code="PROVIDER_GEO_BLOCKED",
                )

            if leida.consecuencia is Consecuencia.AUTENTICACION and not renovado:
                renovado = True
                self._auth.invalidar()
                continue

            if leida.consecuencia is Consecuencia.AUTENTICACION:
                raise CredencialesLinntaeInvalidas(
                    provider=self.slug,
                    message=(
                        "Linntae sigue rechazando el token despues de "
                        "renovarlo una vez. No se insiste."
                    ),
                    external_code="401",
                )

            if leida.http_status >= 500:
                # Un 5xx en una LECTURA es un fallo de transporte, no una
                # respuesta de negocio, y por eso se reintenta y al final se
                # levanta en vez de devolverse.
                #
                # Devolverlo como respuesta seria el fallo silencioso clasico:
                # quien llame vería ``ok == False`` con ``code`` vacio y no
                # podria distinguir "Linntae dijo que no" de "Linntae no
                # contesto". La primera es informacion; la segunda es una
                # ausencia de informacion, y en este sistema se tratan
                # distinto.
                if intento < intentos:
                    time.sleep(min(0.5 * intento, 2.0))
                    continue
                raise ProviderTransientError(
                    provider=self.slug,
                    message=(
                        f"Linntae devolvio {leida.http_status} en {endpoint} "
                        f"en {intentos} intento(s): "
                        f"{leida.mensaje or 'sin mensaje'}."
                    ),
                    external_code=str(leida.http_status),
                )

            if leida.consecuencia is Consecuencia.PERMISOS:
                raise ProviderPermanentError(
                    provider=self.slug,
                    message=f"Linntae niega el permiso en {endpoint}: {leida.mensaje}",
                    external_code=str(leida.http_status),
                )

            if (
                leida.consecuencia is Consecuencia.NO_EJECUTADA_TRANSITORIA
                and intento < intentos
            ):
                # code 23: sistema en mantenimiento, "reintente en dos
                # minutos". Se reintenta dentro de esta llamada solo porque es
                # una lectura; en una compra esto no ocurre.
                time.sleep(min(0.5 * intento, 2.0))
                continue

            return leida

        raise ProviderTransientError(  # pragma: no cover - inalcanzable
            provider=self.slug,
            message=f"Agotados los reintentos de lectura en {endpoint}: {ultimo}",
        )

    # -- compra ------------------------------------------------------------

    def comprar(
        self, endpoint: Endpoint, cuerpo: Mapping[str, Any]
    ) -> RespuestaLinntae:
        """Operacion que mueve dinero. **Un solo intento. Nunca dos.**

        Fijate en lo que falta en este metodo: no hay bucle, no hay contador
        de intentos y no hay renovacion de token con repeticion. Esa ausencia
        es la garantia. La forma de duplicar una recarga es reintentarla sin
        saber que paso con la primera, y aqui no existe el codigo que podria
        hacerlo.

        El token se renueva ANTES de enviar, si le queda poca vida, para que
        un 401 a mitad de camino sea aun mas raro.

        Reparto de los fallos de red, que no son todos iguales:

        * ``ConnectTimeout`` / ``ConnectError``: no se llego a abrir la
          conexion, asi que el cuerpo no salio. Es seguro decir que no se
          ejecuto.
        * cualquier otro fallo despues de eso -lectura, escritura, conexion
          cortada- deja el resultado **desconocido**. Linntae pudo haber
          procesado la recarga y perdido la respuesta.
        """
        self.exigir_ambiente_coherente()

        if not self.config.permitir_operaciones_reales:
            raise ProviderNotConfigured(
                provider=self.slug,
                message=(
                    "ALLOW_REAL_PROVIDER_TRANSACTIONS no esta habilitado, asi "
                    "que no se envia ninguna operacion que mueva saldo a "
                    "Linntae. Es el interruptor final, deliberadamente "
                    "separado del ambiente: tener bien configurado el sandbox "
                    "no autoriza a operar."
                ),
                missing_requirements=("ALLOW_REAL_PROVIDER_TRANSACTIONS=true",),
            )

        # Token fresco antes de gastar saldo.
        self._auth.token(margen_segundos=MARGEN_TOKEN_COMPRA_SEGUNDOS)

        try:
            respuesta = self._enviar(endpoint, cuerpo=cuerpo, metodo="POST")
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            log.warning(
                "linntae_compra_sin_conexion",
                endpoint=str(endpoint),
                error=type(exc).__name__,
            )
            raise ProviderTransientError(
                provider=self.slug,
                message=(
                    "No se pudo abrir la conexion con Linntae, asi que la "
                    "peticion de compra no salio. No se ejecuto nada."
                ),
            ) from exc
        except httpx.HTTPError as exc:
            # Incluye ReadTimeout, WriteTimeout, RemoteProtocolError y
            # ReadError. Todos ocurren DESPUES de empezar a enviar.
            log.error(
                "linntae_compra_indeterminada",
                endpoint=str(endpoint),
                error=type(exc).__name__,
            )
            raise ProviderIndeterminateError(
                provider=self.slug,
                message=(
                    f"Linntae dejo de responder ({type(exc).__name__}) despues "
                    "de recibir la peticion de compra. NO se sabe si la "
                    "recarga se aplico. No se reintenta: se consulta."
                ),
            ) from exc

        return self._interpretar(endpoint, respuesta, es_compra=True)

    # -- internos ----------------------------------------------------------

    def _enviar(
        self,
        endpoint: Endpoint,
        *,
        cuerpo: Mapping[str, Any] | None,
        metodo: str,
    ) -> httpx.Response:
        cabeceras = {"Authorization": self._auth.token().cabecera}
        inicio = time.perf_counter()

        if metodo.upper() == "GET":
            respuesta = self._httpx().get(str(endpoint), headers=cabeceras)
        else:
            respuesta = self._httpx().post(
                str(endpoint), json=dict(cuerpo or {}), headers=cabeceras
            )

        log.info(
            "linntae_peticion",
            endpoint=str(endpoint),
            metodo=metodo.upper(),
            http=respuesta.status_code,
            ms=int((time.perf_counter() - inicio) * 1000),
            # Solo las LLAVES del cuerpo. Los valores incluyen el telefono del
            # cliente y no tienen por que quedar escritos.
            campos=sorted(cuerpo or {}),
            telefono=mask_phone(str((cuerpo or {}).get("phoneNumber") or "")) or "",
        )
        return respuesta

    def _interpretar(
        self, endpoint: Endpoint, respuesta: httpx.Response, *, es_compra: bool
    ) -> RespuestaLinntae:
        payload = self._decodificar(respuesta)
        mensaje = str(payload.get("message") or "")
        code = self._entero(payload.get("code"))

        por_http = consecuencia_http(
            respuesta.status_code, mensaje=mensaje, es_compra=es_compra
        )
        if por_http is not None:
            return RespuestaLinntae(
                http_status=respuesta.status_code,
                code=code,
                mensaje=mensaje or f"Linntae devolvio HTTP {respuesta.status_code}.",
                payload=payload,
                consecuencia=por_http,
            )

        consecuencia = (
            consecuencia_de_compra(code) if es_compra else consecuencia_de_consulta(code)
        )
        if code == 4 and not es_compra:
            mensaje = significado_code_4(endpoint, mensaje)

        return RespuestaLinntae(
            http_status=respuesta.status_code,
            code=code,
            mensaje=mensaje,
            payload=payload,
            consecuencia=consecuencia,
        )

    @staticmethod
    def _decodificar(respuesta: httpx.Response) -> dict[str, Any]:
        """JSON o diccionario vacio. Nunca se interpreta texto a la fuerza."""
        if not respuesta.content:
            return {}
        try:
            datos = respuesta.json()
        except ValueError:
            return {"message": respuesta.text[:300]}
        if isinstance(datos, dict):
            return datos
        return {"data": datos}

    @staticmethod
    def _entero(crudo: Any) -> int | None:
        if crudo is None or isinstance(crudo, bool):
            return None
        try:
            return int(str(crudo).strip())
        except (TypeError, ValueError):
            return None
