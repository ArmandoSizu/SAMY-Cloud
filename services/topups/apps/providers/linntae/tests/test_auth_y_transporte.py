"""Token, guardas de ambiente y politica de reintentos.

Ninguna prueba de este archivo toca la red: el transporte se sustituye por un
``httpx.MockTransport``, asi que se puede ejercitar el comportamiento ante un
500, un 503, un timeout y un 401 sin que exista un servidor.

Lo que estas pruebas protegen, en una frase cada una:

* Un token no se pide dos veces si uno vale.
* Un token nunca aparece en un log ni en un traceback.
* Un 401 no produce un bucle de renovacion.
* ``LINNTAE_ENV=demo`` no puede hablar con produccion.
* Una LECTURA se reintenta y una COMPRA no. Nunca.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx
from django.core.cache import cache
from django.test import SimpleTestCase

from apps.providers.linntae import auth
from apps.providers.linntae.client import (
    URL_PRODUCCION,
    LinntaeClient,
    LinntaeConfig,
)
from apps.providers.linntae.codigos import Consecuencia, Endpoint
from samy_common.providers.exceptions import (
    ProviderIndeterminateError,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTransientError,
)

URL_DEMO_PRUEBA = "https://apidemo.linn.mx/api/v1/"
USUARIO_FALSO = "usuario-de-prueba"
#: Credencial de mentira. No es de nadie y no abre nada.
SECRETO_FALSO = "no-es-una-contrasena-real"
TOKEN_FALSO = "eyJwcmluY2lwYWwiOiJQUlVFQkEifQ"


def _config(**kwargs: Any) -> LinntaeConfig:
    base: dict[str, Any] = {
        "base_url": URL_DEMO_PRUEBA,
        "username": USUARIO_FALSO,
        "password": SECRETO_FALSO,
        "ambiente": "demo",
        "type_balance": 1,
        "permitir_operaciones_reales": True,
        "reintentos_lectura": 2,
    }
    base.update(kwargs)
    return LinntaeConfig(**base)


def _cliente(
    manejador: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> LinntaeClient:
    """Cliente de Linntae con el transporte sustituido por un doble."""
    cliente = LinntaeClient(_config(**kwargs))
    cliente._cliente = httpx.Client(  # noqa: SLF001 - inyeccion deliberada
        base_url=cliente.config.base_url,
        transport=httpx.MockTransport(manejador),
        headers={"Accept": "application/json"},
    )
    return cliente


def _respuesta_token() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "message": "Token generado",
            "token": TOKEN_FALSO,
            "supportId": 27,
        },
    )


class Base(SimpleTestCase):
    def setUp(self) -> None:
        # La cache del token es compartida entre procesos por diseno, asi que
        # entre pruebas hay que limpiarla o una prueba veria el token de otra.
        cache.clear()


class ElTokenNoSeFiltra(Base):
    def test_repr_no_contiene_el_token(self) -> None:
        """Sin este ``__repr__``, un traceback deja la credencial en disco.

        Con ``dataclass`` el repr por omision imprime todos los campos, asi
        que esto pasa sin que nadie lo escriba a proposito.
        """
        token = auth.TokenLinntae(valor=TOKEN_FALSO, support_id=27)
        self.assertNotIn(TOKEN_FALSO, repr(token))
        self.assertNotIn(TOKEN_FALSO, str(token))
        self.assertIn("len=", repr(token))

    def test_un_token_vacio_no_es_un_token(self) -> None:
        with self.assertRaises(ValueError):
            auth.TokenLinntae(valor="")


class CacheDelToken(Base):
    def test_no_se_pide_dos_veces_si_uno_vale(self) -> None:
        llamadas: list[str] = []

        def manejador(peticion: httpx.Request) -> httpx.Response:
            llamadas.append(peticion.url.path)
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            return httpx.Response(200, json={"code": 0, "plataforma": "$100.00"})

        cliente = _cliente(manejador)
        cliente.leer(Endpoint.SALDO, metodo="POST")
        cliente.leer(Endpoint.SALDO, metodo="POST")

        tokens = [ruta for ruta in llamadas if ruta.endswith("getToken")]
        self.assertEqual(len(tokens), 1, f"Se pidio el token {len(tokens)} veces.")

    def test_antes_de_una_compra_se_renueva_un_token_por_caducar(self) -> None:
        """Un 401 a mitad de una compra deja el resultado indeterminado.

        Resolverlo cuesta una conciliacion manual, asi que se gasta una
        llamada en renovar un token al que le queda poco. Esta prueba mete en
        cache un token viejo y comprueba que la compra pide otro.
        """
        llamadas: list[str] = []

        def manejador(peticion: httpx.Request) -> httpx.Response:
            llamadas.append(peticion.url.path)
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            return httpx.Response(
                200, json={"code": 0, "authorization": "611701526", "message": "ok"}
            )

        cliente = _cliente(manejador, token_ttl_segundos=120)
        viejo = auth.TokenLinntae(
            valor="token-viejo", support_id=1, obtenido_en=time.time() - 110
        )
        cache.set(cliente.auth._llave, viejo, timeout=120)  # noqa: SLF001

        cliente.comprar(
            Endpoint.COMPRA_TAE,
            {"idOffer": 1, "phoneNumber": "5512345678", "typeBalance": 1, "extraComision": 0},
        )

        self.assertTrue(
            any(ruta.endswith("getToken") for ruta in llamadas),
            "La compra uso un token a punto de caducar sin renovarlo.",
        )

    def test_credenciales_incompletas_no_llegan_a_la_red(self) -> None:
        def manejador(peticion: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("No deberia hacerse ninguna peticion.")

        cliente = _cliente(manejador, username="", password="")
        with self.assertRaises(ProviderNotConfigured):
            cliente.auth.token()


class CredencialesRechazadas(Base):
    def test_un_401_al_autenticar_es_permanente(self) -> None:
        """Insistir con las mismas credenciales da el mismo resultado.

        Y contra un proveedor que bloquea cuentas por intentos fallidos,
        insistir es peor que fallar.
        """

        def manejador(peticion: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json={"code": 401, "message": "Error al validar el usuario"}
            )

        cliente = _cliente(manejador)
        with self.assertRaises(auth.CredencialesLinntaeInvalidas) as caja:
            cliente.auth.token()
        self.assertFalse(caja.exception.retryable)

    def test_code_4_al_autenticar_es_credencial_invalida(self) -> None:
        def manejador(peticion: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": 4, "message": "Datos de usuario o contrasena incorrectos"},
            )

        cliente = _cliente(manejador)
        with self.assertRaises(auth.CredencialesLinntaeInvalidas):
            cliente.auth.token()

    def test_el_geobloqueo_no_se_confunde_con_credenciales(self) -> None:
        """Linntae usa el mismo 403 para las dos cosas.

        Tratar un geobloqueo como token vencido produce un bucle de
        renovacion; tratarlo como error de credenciales hace buscar en el
        sitio equivocado. El mensaje es lo unico que los distingue.
        """

        def manejador(peticion: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"code": 403, "message": "Error country-US-403"})

        cliente = _cliente(manejador)
        with self.assertRaises(ProviderPermanentError) as caja:
            cliente.auth.token()
        self.assertEqual(caja.exception.external_code, "PROVIDER_GEO_BLOCKED")


class RenovacionSinBucle(Base):
    def test_un_401_en_lectura_renueva_el_token_una_vez(self) -> None:
        estado = {"tokens": 0, "lecturas": 0}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                estado["tokens"] += 1
                return _respuesta_token()
            estado["lecturas"] += 1
            if estado["lecturas"] == 1:
                return httpx.Response(401, json={"code": 401, "message": "sesion"})
            return httpx.Response(200, json={"code": 0, "plataforma": "$50.00"})

        cliente = _cliente(manejador)
        respuesta = cliente.leer(Endpoint.SALDO, metodo="POST")

        self.assertTrue(respuesta.ok)
        self.assertEqual(estado["tokens"], 2, "No se renovo el token tras el 401.")

    def test_un_segundo_401_no_produce_un_bucle(self) -> None:
        """Si las credenciales son malas, "pide token y reintenta" no termina.

        Contra un proveedor que puede bloquear cuentas por abuso, ese bucle no
        es una molestia: es como se pierde el acceso.
        """
        estado = {"tokens": 0, "lecturas": 0}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                estado["tokens"] += 1
                return _respuesta_token()
            estado["lecturas"] += 1
            return httpx.Response(401, json={"code": 401, "message": "sesion"})

        cliente = _cliente(manejador)
        with self.assertRaises(auth.CredencialesLinntaeInvalidas):
            cliente.leer(Endpoint.SALDO, metodo="POST")

        self.assertLessEqual(estado["tokens"], 2)
        self.assertLessEqual(estado["lecturas"], 2)


class GuardaDeAmbiente(Base):
    def test_demo_no_puede_hablar_con_produccion(self) -> None:
        """Es la forma mas directa de mandar una recarga real por error.

        Aqui es imposible: el adaptador se niega antes de abrir la conexion.
        """

        def manejador(peticion: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("No deberia hacerse ninguna peticion.")

        cliente = _cliente(manejador, ambiente="demo", base_url=URL_PRODUCCION)
        with self.assertRaises(ProviderNotConfigured) as caja:
            cliente.leer(Endpoint.SALDO, metodo="POST")
        self.assertIn("apidemo.linn.mx", caja.exception.message)

    def test_produccion_no_puede_hablar_con_demo(self) -> None:
        def manejador(peticion: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("No deberia hacerse ninguna peticion.")

        cliente = _cliente(manejador, ambiente="production", base_url=URL_DEMO_PRUEBA)
        with self.assertRaises(ProviderNotConfigured):
            cliente.leer(Endpoint.SALDO, metodo="POST")

    def test_solo_https(self) -> None:
        fallos = _config(base_url="http://apidemo.linn.mx/api/v1/").problemas_de_ambiente()
        self.assertTrue(any("HTTPS" in f for f in fallos))

    def test_un_ambiente_desconocido_no_se_interpreta(self) -> None:
        fallos = _config(ambiente="pruebas").problemas_de_ambiente()
        self.assertTrue(any("no se reconoce" in f for f in fallos))

    def test_se_reportan_todos_los_problemas_no_el_primero(self) -> None:
        """Quien configura esto quiere arreglarlo de una vez."""
        fallos = _config(ambiente="pruebas", base_url="http://otro.test/").problemas_de_ambiente()
        self.assertGreaterEqual(len(fallos), 2)


class LecturasSeReintentan(Base):
    def test_un_500_en_lectura_se_reintenta(self) -> None:
        estado = {"lecturas": 0}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            estado["lecturas"] += 1
            if estado["lecturas"] < 3:
                return httpx.Response(500, json={"code": 500, "message": "Oops"})
            return httpx.Response(200, json={"code": 0, "plataforma": "$10.00"})

        cliente = _cliente(manejador)
        respuesta = cliente.leer(Endpoint.SALDO, metodo="POST")
        self.assertTrue(respuesta.ok)
        self.assertEqual(estado["lecturas"], 3)

    def test_un_timeout_en_lectura_se_reintenta_y_acaba_en_transitorio(self) -> None:
        estado = {"intentos": 0}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            estado["intentos"] += 1
            raise httpx.ReadTimeout("lento", request=peticion)

        cliente = _cliente(manejador)
        with self.assertRaises(ProviderTransientError):
            cliente.leer(Endpoint.SALDO, metodo="POST")
        self.assertEqual(estado["intentos"], 3)


class LasComprasNoSeReintentan(Base):
    """La garantia es estructural: ``comprar()`` no tiene bucle."""

    def _compra(self) -> dict[str, Any]:
        return {
            "idOffer": 1,
            "phoneNumber": "5512345678",
            "typeBalance": 1,
            "extraComision": 0,
        }

    def test_un_500_en_compra_no_se_reintenta_y_es_indeterminado(self) -> None:
        """500 despues de mandar una compra NO significa que fallo.

        Si esta prueba se cae, alguien metio un reintento en el camino de la
        compra, y eso es como se aplica y se paga una recarga dos veces.
        """
        estado = {"compras": 0}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            estado["compras"] += 1
            return httpx.Response(500, json={"code": 500, "message": "Error al realizar"})

        cliente = _cliente(manejador)
        respuesta = cliente.comprar(Endpoint.COMPRA_TAE, self._compra())
        self.assertEqual(estado["compras"], 1)
        self.assertIs(respuesta.consecuencia, Consecuencia.INDETERMINADA)

    def test_un_503_en_compra_es_indeterminado(self) -> None:
        """Su 503 dice "Existe una transaccion en proceso".

        Eso es lo mas parecido a una confesion de que algo esta corriendo del
        otro lado.
        """

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            return httpx.Response(
                503,
                json={
                    "code": 503,
                    "message": "Existe una transaccion en proceso, vuelve a intentarlo en 1 minuto",
                },
            )

        cliente = _cliente(manejador)
        respuesta = cliente.comprar(Endpoint.COMPRA_TAE, self._compra())
        self.assertIs(respuesta.consecuencia, Consecuencia.INDETERMINADA)

    def test_un_readtimeout_en_compra_es_indeterminado(self) -> None:
        estado = {"compras": 0}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            estado["compras"] += 1
            raise httpx.ReadTimeout("sin respuesta", request=peticion)

        cliente = _cliente(manejador)
        with self.assertRaises(ProviderIndeterminateError):
            cliente.comprar(Endpoint.COMPRA_TAE, self._compra())
        self.assertEqual(estado["compras"], 1)

    def test_un_connecttimeout_en_compra_si_es_no_ejecutada(self) -> None:
        """No se abrio la conexion, asi que el cuerpo no salio.

        Es el unico fallo de red del que se puede afirmar que nada ocurrio, y
        distinguirlo evita mandar a revision manual algo que solo fue una red
        caida.
        """

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            raise httpx.ConnectTimeout("sin conexion", request=peticion)

        cliente = _cliente(manejador)
        with self.assertRaises(ProviderTransientError):
            cliente.comprar(Endpoint.COMPRA_TAE, self._compra())

    def test_sin_la_bandera_no_se_envia_ninguna_compra(self) -> None:
        """El interruptor final. Separado del ambiente a proposito."""
        estado = {"compras": 0}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            estado["compras"] += 1
            return httpx.Response(200, json={"code": 0, "authorization": "1"})

        cliente = _cliente(manejador, permitir_operaciones_reales=False)
        with self.assertRaises(ProviderNotConfigured) as caja:
            cliente.comprar(Endpoint.COMPRA_TAE, self._compra())
        self.assertIn("ALLOW_REAL_PROVIDER_TRANSACTIONS", caja.exception.message)
        self.assertEqual(estado["compras"], 0)

    def test_no_se_manda_idempotency_key_a_purchase_tae(self) -> None:
        """Su especificacion la documenta SOLO en /purchase/pin.

        Mandarla aqui suponiendo que funciona seria construir la proteccion
        contra duplicados sobre una cabecera que quiza ignoran.
        """
        cabeceras: dict[str, str] = {}

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return _respuesta_token()
            cabeceras.update(dict(peticion.headers))
            return httpx.Response(200, json={"code": 0, "authorization": "611701526"})

        cliente = _cliente(manejador)
        cliente.comprar(Endpoint.COMPRA_TAE, self._compra())
        self.assertNotIn("idempotency-key", {k.lower() for k in cabeceras})
