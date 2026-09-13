"""Codigos de Linntae, traducidos a consecuencias de DINERO.

Este modulo no traduce codigos a mensajes. Traduce codigos a la unica pregunta
que importa despues de mandar una recarga:

    ¿Se ejecuto, no se ejecuto, o no se sabe?

Y existe separado del cliente HTTP porque la respuesta a esa pregunta es la
que decide si se cobra, si se reembolsa o si una persona tiene que mirarlo.
Enterrada dentro de un ``if`` en el adaptador seria imposible de auditar y de
probar sin red.

LO QUE LA ESPECIFICACION DICE, Y LO QUE NO
------------------------------------------

La especificacion OpenAPI de Linntae (v2.0.0) declara los codigos dentro de un
HTTP 200. Un 200 NO significa exito: significa que la conexion funciono. El
exito es ``code == 0``.

Un detalle que obliga a no tener una tabla global: **el codigo 4 significa
cosas distintas segun el endpoint**.

    /getToken          code 4 = usuario o contrasena incorrectos
    /sale/unlock       code 4 = no existe una venta relacionada
    /purchase/pin      code 4 = venta de pines bloqueada
    /config/syncSchedule  code 4 = no hay informacion disponible

Por eso la traduccion se pide siempre indicando el endpoint.

EL CRITERIO CON LOS AMBIGUOS
----------------------------

Dos codigos parecen fallos y se tratan distinto, y la diferencia es
deliberada:

``code 23`` (sistema en mantenimiento)
    Aparece tambien en los endpoints de solo lectura (``/products/*``). Eso
    es la prueba de que es una puerta global del sistema y no el desenlace de
    una venta: si el sistema esta en mantenimiento, la venta no entro. Se
    trata como NO EJECUTADA.

``code 22`` (la compania esta presentando fallas)
    Aparece SOLO en los endpoints de compra y nombra a una compania concreta
    (`"La compañia Telcel esta presentando fallas"`). Es el desenlace de un
    intento, no una puerta previa, y la especificacion no dice en que momento
    fallo. Puede haber llegado al operador. Se trata como **INDETERMINADA**.

El costo de equivocarse no es simetrico y por eso el empate se rompe hacia la
duda: dar por fallida una recarga que si se aplico significa reembolsar a un
cliente que ya tiene su saldo, y ese dinero no se recupera. Dar por
indeterminada una que fallo cuesta una consulta.
"""

from __future__ import annotations

import enum
import re
from typing import Final

__all__ = [
    "Consecuencia",
    "Endpoint",
    "CODIGO_EXITO",
    "consecuencia_de_compra",
    "consecuencia_de_consulta",
    "consecuencia_http",
    "es_geobloqueo",
    "significado_code_4",
]

#: El unico codigo que Linntae considera exito.
CODIGO_EXITO: Final[int] = 0


class Endpoint(enum.StrEnum):
    """Endpoints de Linntae que este adaptador usa.

    Los nombres estan escritos **exactamente** como los publica Linntae,
    incluido ``checkTransacctionTae`` con la doble "c". No es un error de
    transcripcion: es la ruta real y corregirla produciria un 404.
    """

    TOKEN = "getToken"
    SALDO = "balance/getBalance"
    COMPANIAS_TAE = "products/taeCompanies"
    COMPANIAS_VIRTUALES = "products/taeVirtualCompanies"
    SINCRONIZAR_PRODUCTOS = "config/syncProducts"
    COMISIONES = "config/getProductsCommissions"
    ESQUEMA = "config/getScheme"
    COMPRA_TAE = "purchase/tae"
    CONSULTA_TAE = "sale/checkTransacctionTae"
    VENTAS = "sale/list"


class Consecuencia(enum.StrEnum):
    """Que le paso al dinero. Es lo unico que el resto del sistema necesita.

    ``EJECUTADA``
        Linntae confirmo la operacion. Hay autorizacion.

    ``NO_EJECUTADA``
        Rechazo definitivo ANTES de tocar al operador: datos invalidos, tipo
        de saldo no disponible, saldo insuficiente. Nada se movio. Es seguro
        cerrarla como fallida y reembolsar.

    ``NO_EJECUTADA_TRANSITORIA``
        Rechazo por una puerta global (mantenimiento). Nada se movio, pero la
        causa es temporal. Distinguirla de la anterior sirve para el mensaje y
        para las metricas; para el dinero son lo mismo.

    ``INDETERMINADA``
        No se sabe. Timeout, 500, 503, o la compania con intermitencia.
        **Jamas se reintenta sola** y **jamas se cierra por suposicion**: se
        consulta el estado real.

    ``DUPLICADA``
        Linntae dice que ya existe una venta igual hoy (code 24). No es un
        error: es informacion. Puede que la anterior si se aplicara. Hay que
        averiguarlo antes de hacer cualquier otra cosa.

    ``AUTENTICACION``
        El token no sirve. Se renueva una vez y se reintenta una vez, solo en
        operaciones de lectura.

    ``PERMISOS``
        La cuenta no tiene permiso, esta bloqueada o inactiva. No se
        reintenta.

    ``GEOBLOQUEO``
        Linntae rechaza el pais de origen de la peticion. No se reintenta
        nunca: reintentar desde la misma IP da el mismo resultado y solo
        gasta llamadas.
    """

    EJECUTADA = "EJECUTADA"
    NO_EJECUTADA = "NO_EJECUTADA"
    NO_EJECUTADA_TRANSITORIA = "NO_EJECUTADA_TRANSITORIA"
    INDETERMINADA = "INDETERMINADA"
    DUPLICADA = "DUPLICADA"
    AUTENTICACION = "AUTENTICACION"
    PERMISOS = "PERMISOS"
    GEOBLOQUEO = "GEOBLOQUEO"


#: Codigos de negocio en una COMPRA (``/purchase/tae``), segun la
#: especificacion. Lo que no este aqui es INDETERMINADO, no fallido: un codigo
#: que no reconocemos en una respuesta de compra puede perfectamente venir de
#: una venta aplicada.
_COMPRA: Final[dict[int, Consecuencia]] = {
    0: Consecuencia.EJECUTADA,
    # "Producto invalido", "El numero de telefono debe de ser de 10 digitos",
    # "La ganancia debe ser..." — validaciones de entrada. No entro al
    # operador.
    1: Consecuencia.NO_EJECUTADA,
    # "Tipo de saldo seleccionado no disponible".
    2: Consecuencia.NO_EJECUTADA,
    # "Saldo plataforma insuficiente". Nada se movio: no habia con que.
    3: Consecuencia.NO_EJECUTADA,
    # "La compania X esta presentando fallas". Ver el docstring del modulo.
    22: Consecuencia.INDETERMINADA,
    # "Sistema en mantenimiento". Puerta global.
    23: Consecuencia.NO_EJECUTADA_TRANSITORIA,
    # "Recarga duplicada del dia de hoy con telefono ... y con monto ...".
    24: Consecuencia.DUPLICADA,
    # Solo documentado en /purchase/pin: la compra SI fue autorizada y el
    # codigo sigue en recuperacion. ``retrySale`` viene en false. Se incluye
    # porque si apareciera en TAE, tratarlo como fallo cerraria como fallida
    # una operacion que la propia respuesta declara autorizada.
    25: Consecuencia.EJECUTADA,
}

#: Codigos en una CONSULTA de solo lectura. Aqui no hay dinero en juego, asi
#: que lo desconocido puede tratarse como fallo de lectura sin riesgo.
_CONSULTA: Final[dict[int, Consecuencia]] = {
    0: Consecuencia.EJECUTADA,
    1: Consecuencia.NO_EJECUTADA,
    2: Consecuencia.NO_EJECUTADA,
    4: Consecuencia.NO_EJECUTADA,
    8: Consecuencia.NO_EJECUTADA,
    23: Consecuencia.NO_EJECUTADA_TRANSITORIA,
}

#: ``code 4`` no tiene un significado unico. Esta tabla existe para que el
#: mensaje que se registra sea el correcto y para que nadie escriba una
#: traduccion global de un codigo que no la tiene.
_CODE_4: Final[dict[Endpoint, str]] = {
    Endpoint.TOKEN: "Usuario o contrasena de Linntae incorrectos.",
    Endpoint.ESQUEMA: "Linntae no reporta esquema de comisiones para esta cuenta.",
}

#: ``Error country-US-403``: Linntae bloquea por pais de origen. El patron
#: captura el codigo de pais para poder reportarlo sin inventarlo.
_GEO: Final[re.Pattern[str]] = re.compile(r"country-([A-Za-z]{2})-403", re.IGNORECASE)


def es_geobloqueo(mensaje: str) -> str | None:
    """Codigo de pais si el mensaje es un bloqueo geografico, ``None`` si no.

    Importa distinguirlo de un token invalido porque Linntae usa **el mismo
    HTTP 403 para los dos**. Tratar un geobloqueo como token vencido haria
    que el adaptador pidiera un token nuevo, volviera a recibir 403, y
    repitiera. Tratar un token vencido como geobloqueo dejaria la integracion
    caida hasta que alguien reiniciara el servicio.
    """
    if not mensaje:
        return None
    encontrado = _GEO.search(mensaje)
    return encontrado.group(1).upper() if encontrado else None


def significado_code_4(endpoint: Endpoint, mensaje: str = "") -> str:
    """Texto de ``code 4`` para ese endpoint. Nunca inventa uno generico."""
    conocido = _CODE_4.get(endpoint)
    if conocido:
        return conocido
    return mensaje or f"Linntae devolvio code 4 en {endpoint}, sin mensaje."


def consecuencia_de_compra(code: int | None) -> Consecuencia:
    """Que le paso al dinero, segun el codigo de una COMPRA.

    Un codigo ausente o desconocido es ``INDETERMINADA``. Esa es la decision
    central de este modulo: en una respuesta de compra, "no entiendo lo que me
    dijiste" no autoriza a afirmar que no paso nada.
    """
    if code is None:
        return Consecuencia.INDETERMINADA
    return _COMPRA.get(int(code), Consecuencia.INDETERMINADA)


def consecuencia_de_consulta(code: int | None) -> Consecuencia:
    """Que paso, segun el codigo de una CONSULTA de solo lectura."""
    if code is None:
        return Consecuencia.NO_EJECUTADA
    return _CONSULTA.get(int(code), Consecuencia.NO_EJECUTADA)


def consecuencia_http(
    status: int, *, mensaje: str = "", es_compra: bool
) -> Consecuencia | None:
    """Consecuencia derivada del codigo HTTP, o ``None`` si hay que leer el cuerpo.

    ``es_compra`` cambia el resultado de 500 y 503, y esa es la parte
    importante:

    * En una LECTURA, un 500 es un fallo de lectura. No hay dinero.
    * En una COMPRA, un 500 o un 503 **no significan que la recarga fallo**.
      Significan que no llego una respuesta util. El 503 de Linntae dice
      literalmente "Existe una transaccion en proceso": eso es lo mas
      parecido a una confesion de que algo esta corriendo del otro lado.
    """
    if status == 401:
        return Consecuencia.AUTENTICACION

    if status == 403:
        return Consecuencia.GEOBLOQUEO if es_geobloqueo(mensaje) else Consecuencia.PERMISOS

    if status == 422:
        return Consecuencia.NO_EJECUTADA

    if status in (500, 503) or status >= 500:
        return Consecuencia.INDETERMINADA if es_compra else Consecuencia.NO_EJECUTADA

    if 400 <= status < 500:
        # 404 y compania: la ruta no existe o la peticion esta mal formada.
        # Nada se ejecuto, ni en compra.
        return Consecuencia.NO_EJECUTADA

    # 2xx: la respuesta HTTP esta bien y el veredicto vive en ``code``.
    return None
