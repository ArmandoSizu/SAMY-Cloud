"""Autenticacion contra Linntae: politica de token, sin transporte HTTP.

Linntae autentica con ``POST /getToken`` (usuario y contrasena) y despues
espera ``Authorization: Bearer TOKEN`` en todo lo demas.

Este modulo decide **cuando** pedir un token y **cuando dejar de creer en el
que se tiene**. No sabe hacer peticiones HTTP: recibe una funcion que las
hace. Esa separacion permite probar toda la politica -caducidad, renovacion,
tope de reintentos, cache compartida- sin abrir un socket.

LO QUE LA ESPECIFICACION NO DICE
--------------------------------

**No publica la vida del token.** Ni en ``/getToken`` ni en ninguna parte
aparece un ``expires_in``. Asi que el TTL de esta cache es NUESTRO, no suyo,
y por eso es corto y configurable. La consecuencia de equivocarse a la baja
es una llamada de mas; a la alta, un 401 en mitad de una operacion. Se
prefiere la primera.

Y precisamente porque el TTL es una suposicion, el token tambien se invalida
por evidencia: un 401 de Linntae vale mas que cualquier reloj nuestro.

EL CICLO INFINITO QUE ESTE MODULO EVITA
---------------------------------------

La forma ingenua de manejar un 401 es "pide token y reintenta". Si las
credenciales son malas, o si la cuenta esta bloqueada, o si el 403 de Linntae
resulta ser geografico, eso reintenta para siempre: token nuevo, 401, token
nuevo, 401. Contra un proveedor que puede bloquear cuentas por abuso, eso no
es un bucle, es una forma de perder el acceso.

Aqui la renovacion por 401 esta acotada a **un** intento por operacion, y el
contador lo lleva quien llama (``client.py``), no la cache. Un segundo 401
seguido se propaga como error permanente.

EL TOKEN NUNCA SE REGISTRA
--------------------------

``TokenLinntae`` define su propio ``__repr__``. No es cosmetica: sin el,
cualquier ``repr()`` en un traceback, en un log estructurado o en un mensaje
de excepcion escribiria el token completo en disco. Con dataclasses eso pasa
sin que nadie lo escriba a proposito.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable, Final

import structlog
from django.core.cache import cache

from samy_common.providers.exceptions import (
    ProviderNotConfigured,
    ProviderPermanentError,
)

__all__ = [
    "TokenLinntae",
    "Autenticador",
    "CredencialesLinntaeInvalidas",
    "PREFIJO_CACHE",
    "TTL_POR_OMISION",
]

log = structlog.get_logger("provider.linntae.auth")

PREFIJO_CACHE: Final[str] = "samy:topups:linntae:token:"

#: Vida que le damos al token en cache, en segundos. **Es una suposicion
#: nuestra**: Linntae no publica la vida real. Diez minutos deja la ventana de
#: un 401 inesperado corta sin convertir cada venta en dos llamadas.
TTL_POR_OMISION: Final[int] = 600

#: Cuanto se espera, como maximo, a que otro proceso termine de pedir el
#: token. Pasado eso se pide uno propio: un ``getToken`` de mas es barato, y
#: quedarse esperando bloquearia una venta.
_ESPERA_MAXIMA_SEGUNDOS: Final[float] = 2.0
_PASO_DE_ESPERA: Final[float] = 0.1


class CredencialesLinntaeInvalidas(ProviderPermanentError):
    """Linntae rechazo el usuario o la contrasena.

    Permanente a proposito: reintentar con las mismas credenciales da el mismo
    resultado, y contra un proveedor que bloquea cuentas por intentos
    fallidos, insistir es peor que fallar.
    """

    code = "linntae_credenciales_invalidas"


@dataclass(frozen=True, slots=True)
class TokenLinntae:
    """Un token vivo de Linntae.

    ``support_id`` lo devuelve Linntae junto al token y sirve para abrir un
    ticket con su soporte citando la sesion. No es secreto.
    """

    valor: str
    support_id: int | None = None
    #: Momento en que Linntae lo entrego (``time.time()``). Se guarda DENTRO
    #: del token y no en la cache porque la cache de Django no expone el
    #: tiempo restante de una llave de forma portable, y antes de una compra
    #: hace falta saber si al token le queda vida.
    obtenido_en: float = 0.0

    def __post_init__(self) -> None:
        if not self.valor or not self.valor.strip():
            raise ValueError("Un token vacio no es un token.")

    @property
    def edad_segundos(self) -> float:
        """Cuanto lleva vivo. ``0`` si no se registro cuando se obtuvo."""
        if not self.obtenido_en:
            return 0.0
        return max(0.0, time.time() - self.obtenido_en)

    def __repr__(self) -> str:
        """Nunca el token. Solo su longitud y el support id.

        Esta linea es la que impide que un traceback deje la credencial
        escrita en los registros.
        """
        return f"<TokenLinntae len={len(self.valor)} support_id={self.support_id}>"

    def __str__(self) -> str:
        return repr(self)

    @property
    def cabecera(self) -> str:
        return f"Bearer {self.valor}"


class Autenticador:
    """Cache y renovacion del token de Linntae.

    La cache es la de Django, que en este servicio es Redis. Eso importa:
    varios workers de Celery y varios procesos de gunicorn comparten el
    token, asi que una jornada entera no cuesta una autenticacion por proceso.

    La llave incluye una huella de ``base_url`` + usuario. No es adorno: sin
    ella, cambiar de ambiente o de cuenta dejaria el token anterior en la
    cache y la siguiente peticion iria a Linntae con la credencial del
    ambiente equivocado. La huella es un hash: el usuario no se escribe en
    ninguna llave de Redis.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        solicitar: Callable[[str, str], TokenLinntae],
        ttl_segundos: int = TTL_POR_OMISION,
        etiqueta_ambiente: str = "",
    ) -> None:
        self._base_url = base_url
        self._username = username
        self._password = password
        self._solicitar = solicitar
        self._ttl = max(int(ttl_segundos), 30)
        self._etiqueta = etiqueta_ambiente or "sin-ambiente"

    # -- llaves -----------------------------------------------------------

    @property
    def huella(self) -> str:
        """Identidad de esta configuracion, sin revelarla.

        Entra la URL base y el usuario, no la contrasena: la huella viaja a
        los logs y a las llaves de Redis, y una contrasena hasheada sigue
        siendo material que no tiene por que salir del proceso.
        """
        crudo = f"{self._base_url}|{self._username}"
        return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:12]

    @property
    def _llave(self) -> str:
        return f"{PREFIJO_CACHE}{self._etiqueta}:{self.huella}"

    @property
    def _llave_candado(self) -> str:
        return f"{self._llave}:pidiendo"

    @property
    def credenciales_completas(self) -> bool:
        return bool(self._base_url and self._username and self._password)

    # -- API --------------------------------------------------------------

    def token(self, *, margen_segundos: int = 0) -> TokenLinntae:
        """Token valido, de cache si hay, pidiendolo si no.

        ``margen_segundos`` pide un token con al menos esa vida por delante.
        Se usa antes de una COMPRA: un 401 a mitad de una recarga deja el
        resultado indeterminado y eso cuesta una conciliacion manual, asi que
        vale la pena gastar una llamada en renovar un token que esta por
        caducar. En las lecturas el margen es cero: ahi un 401 solo cuesta un
        reintento.
        """
        if not self.credenciales_completas:
            raise ProviderNotConfigured(
                provider="linntae",
                message=(
                    "Faltan credenciales de Linntae. Define LINNTAE_BASE_URL, "
                    "LINNTAE_USERNAME y LINNTAE_PASSWORD en el .env. No hay "
                    "valores por omision: un usuario inventado seria una "
                    "peticion a la cuenta de otra persona."
                ),
                missing_requirements=(
                    "LINNTAE_BASE_URL",
                    "LINNTAE_USERNAME",
                    "LINNTAE_PASSWORD",
                ),
            )

        guardado = cache.get(self._llave)
        if isinstance(guardado, TokenLinntae) and self._le_queda_vida(
            guardado, margen_segundos
        ):
            return guardado

        if isinstance(guardado, TokenLinntae):
            # Esta en cache pero le queda menos que el margen pedido. Se
            # descarta antes de pedir uno nuevo para que, si dos procesos
            # coinciden, ninguno se quede con el viejo.
            cache.delete(self._llave)

        return self._pedir_con_candado()

    def _le_queda_vida(self, token: TokenLinntae, margen_segundos: int) -> bool:
        if margen_segundos <= 0:
            return True
        return token.edad_segundos + margen_segundos <= self._ttl

    def invalidar(self) -> None:
        """Olvida el token guardado. Se llama ante un 401 de Linntae.

        La evidencia de Linntae vale mas que nuestro TTL: si dice que el token
        no sirve, no sirve, aunque nuestro reloj diga que le quedaban ocho
        minutos.
        """
        cache.delete(self._llave)
        log.info("linntae_token_invalidado", huella=self.huella, ambiente=self._etiqueta)

    # -- interno ----------------------------------------------------------

    def _pedir_con_candado(self) -> TokenLinntae:
        """Pide el token evitando que veinte workers lo pidan a la vez.

        El candado es una cortesia, no una garantia: si no se consigue, se
        espera un momento por si otro lo deja en la cache y, si no aparece, se
        pide igual. Un ``getToken`` duplicado no mueve dinero; una venta
        bloqueada esperando un candado si molesta a un cliente en el
        mostrador.
        """
        tengo_candado = cache.add(self._llave_candado, "1", timeout=10)

        if not tengo_candado:
            esperado = 0.0
            while esperado < _ESPERA_MAXIMA_SEGUNDOS:
                time.sleep(_PASO_DE_ESPERA)
                esperado += _PASO_DE_ESPERA
                guardado = cache.get(self._llave)
                if isinstance(guardado, TokenLinntae):
                    return guardado

        try:
            token = self._solicitar(self._username, self._password)
        finally:
            if tengo_candado:
                cache.delete(self._llave_candado)

        cache.set(self._llave, token, timeout=self._ttl)
        log.info(
            "linntae_token_obtenido",
            huella=self.huella,
            ambiente=self._etiqueta,
            ttl_segundos=self._ttl,
            support_id=token.support_id,
            # La longitud sirve para detectar un token truncado por
            # configuracion; el valor no se registra nunca.
            largo=len(token.valor),
        )
        return token
