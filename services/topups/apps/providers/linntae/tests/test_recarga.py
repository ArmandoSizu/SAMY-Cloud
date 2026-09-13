"""La recarga: cada codigo de Linntae y que le pasa al dinero.

Ninguna prueba manda una recarga real. El transporte esta sustituido y la
suite corre con ``ENVIRONMENT=test``, asi que un proveedor en modo PRODUCTION
es rechazado antes de autenticarse.

Lo que estas pruebas fijan, y que es la mitad del valor de esta integracion:

* ``code 0`` sin autorizacion **no** es exito.
* ``code 22``, un 500, un 503 y un timeout dejan la recarga en revision, no
  en fallo.
* ``code 24`` no es un error: es una pregunta abierta.
* NADIE llama a ``/sale/unlock``. En ninguna rama, por ningun motivo.
* ``get_topup_status()`` nunca devuelve FAILED, porque con Linntae no puede
  saberlo.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Callable

import httpx
from django.core.cache import cache
from django.test import SimpleTestCase

from apps.providers.base import ContextoConciliacion, TopupRequest, TopupStatus
from apps.providers.linntae.client import LinntaeClient, LinntaeConfig
from apps.providers.linntae.provider import LinntaeProvider
from samy_common.money import Money
from samy_common.providers.base import ProviderMode, ProviderStatus
from samy_common.providers.exceptions import (
    ProviderIndeterminateError,
    ProviderNotConfigured,
    ProviderPermanentError,
)

URL_DEMO_PRUEBA = "https://apidemo.linn.mx/api/v1/"
SECRETO_FALSO = "no-es-una-contrasena-real"
TOKEN_FALSO = "eyJwcmluY2lwYWwiOiJQUlVFQkEifQ"

TELEFONO = "3121234567"
ID_OFFER = "96"


def _config(**kwargs: Any) -> LinntaeConfig:
    base: dict[str, Any] = {
        "base_url": URL_DEMO_PRUEBA,
        "username": "usuario-de-prueba",
        "password": SECRETO_FALSO,
        "ambiente": "demo",
        "type_balance": 1,
        "extra_comision": 0,
        "permitir_operaciones_reales": True,
        "reintentos_lectura": 0,
    }
    base.update(kwargs)
    return LinntaeConfig(**base)


def _proveedor(
    manejador: Callable[[httpx.Request], httpx.Response],
    *,
    modo: ProviderMode = ProviderMode.SANDBOX,
    **kwargs: Any,
) -> LinntaeProvider:
    proveedor = LinntaeProvider(_config(**kwargs), modo)
    cliente = LinntaeClient(proveedor.config)
    cliente._cliente = httpx.Client(  # noqa: SLF001 - inyeccion deliberada
        base_url=proveedor.config.base_url,
        transport=httpx.MockTransport(manejador),
        headers={"Accept": "application/json"},
    )
    proveedor._client = cliente  # noqa: SLF001
    return proveedor


def _peticion() -> TopupRequest:
    return TopupRequest(
        fulfillment_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        operator_code="1",
        product_id=ID_OFFER,
        amount=Money.parse("100", "MXN"),
        phone_e164=f"+52{TELEFONO}",
        phone_national=TELEFONO,
        phone_masked="31****4567",
        idempotency_key="topup:prueba",
        amount_in_sku=True,
    )


def _manejador(
    compra: httpx.Response | Callable[[httpx.Request], httpx.Response],
    *,
    saldo: str = "$5,000.00",
    rutas: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Doble de Linntae: token, saldo y una respuesta de compra a elegir.

    ``compra`` puede ser una respuesta o una funcion, para poder simular
    tambien un fallo de red -que es la mitad interesante de esta
    integracion- levantando la excepcion con la peticion real.
    """

    def manejador(peticion: httpx.Request) -> httpx.Response:
        ruta = peticion.url.path
        if rutas is not None:
            rutas.append(ruta)
        if ruta.endswith("getToken"):
            return httpx.Response(
                200, json={"code": 0, "message": "ok", "token": TOKEN_FALSO, "supportId": 1}
            )
        if ruta.endswith("balance/getBalance"):
            return httpx.Response(200, json={"code": 0, "plataforma": saldo})
        if ruta.endswith("purchase/tae"):
            return compra(peticion) if callable(compra) else compra
        return httpx.Response(404, json={"code": 404, "message": f"ruta no simulada: {ruta}"})

    return manejador


def _timeout_de_lectura(peticion: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("sin respuesta", request=peticion)


class Base(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()


class Salud(Base):
    def test_con_todo_puesto_esta_listo(self) -> None:
        proveedor = _proveedor(_manejador(httpx.Response(200, json={"code": 0})))
        salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.READY)

    def test_sin_type_balance_no_esta_listo(self) -> None:
        """Y eso es lo correcto: sin typeBalance la recarga no se puede mandar.

        Si esto dijera READY, el producto quedaria vendible y la secuencia
        seria cobrar al cliente y descubrir despues que no se puede entregar.
        """
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0})), type_balance=None
        )
        salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.DEGRADED)
        self.assertIn("LINNTAE_TYPE_BALANCE", salud.detail)

    def test_sin_saldo_no_esta_listo(self) -> None:
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0}), saldo="$0.00")
        )
        salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.DEGRADED)

    def test_un_saldo_ilegible_no_se_lee_como_cero_ni_como_disponible(self) -> None:
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0}), saldo="no disponible")
        )
        salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.DEGRADED)
        self.assertIn("no se reconocio", salud.detail)

    def test_sin_la_bandera_de_operaciones_reales_no_esta_listo(self) -> None:
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0})),
            permitir_operaciones_reales=False,
        )
        salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.DEGRADED)
        self.assertIn("ALLOW_REAL_PROVIDER_TRANSACTIONS", salud.missing_requirements[0])

    def test_el_saldo_disponible_nunca_levanta(self) -> None:
        """La guarda de saldo corre al crear el cumplimiento.

        Una excepcion aqui convertiria un proveedor caido en un 500 en la
        pantalla de caja, en vez de un "hoy no se vende esto".
        """

        def manejador(peticion: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("sin red", request=peticion)

        proveedor = _proveedor(manejador)
        estado = proveedor.saldo_disponible()
        self.assertIsNone(estado.disponible)
        self.assertTrue(estado.detalle)


class RecargaExitosa(Base):
    def test_code_cero_con_autorizacion(self) -> None:
        proveedor = _proveedor(
            _manejador(
                httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "authorization": "611701526",
                        "message": "Pago realizado correctamente",
                    },
                )
            )
        )
        resultado = proveedor.send_topup(_peticion())
        self.assertEqual(resultado.status, TopupStatus.SUCCEEDED)
        self.assertEqual(resultado.provider_reference, "611701526")
        self.assertEqual(resultado.operator_reference, "611701526")

    def test_code_cero_SIN_autorizacion_no_es_exito(self) -> None:
        """Sin autorizacion no hay con que verificar ni con que reclamar.

        Cerrarla como exitosa dejaria una venta que nadie puede auditar y un
        cliente sin folio si el saldo no llego.
        """
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0, "message": "ok"}))
        )
        with self.assertRaises(ProviderIndeterminateError):
            proveedor.send_topup(_peticion())


class RechazosQueNoMovieronNada(Base):
    def test_code_1_2_y_3_son_fallo_definitivo(self) -> None:
        casos = {
            1: "Producto invalido",
            2: "Tipo de saldo seleccionado no disponible",
            3: "Saldo plataforma insuficiente",
        }
        for code, mensaje in casos.items():
            with self.subTest(code=code):
                proveedor = _proveedor(
                    _manejador(httpx.Response(200, json={"code": code, "message": mensaje}))
                )
                resultado = proveedor.send_topup(_peticion())
                self.assertEqual(resultado.status, TopupStatus.FAILED)
                self.assertIn(mensaje, resultado.failure_reason)

    def test_code_23_mantenimiento_es_fallo_no_revision(self) -> None:
        """Aparece tambien en las lecturas: es una puerta global del sistema.

        Si el sistema esta en mantenimiento, la venta no entro.
        """
        proveedor = _proveedor(
            _manejador(
                httpx.Response(
                    200,
                    json={"code": 23, "message": "Sistema en mantenimiento por favor reintente"},
                )
            )
        )
        resultado = proveedor.send_topup(_peticion())
        self.assertEqual(resultado.status, TopupStatus.FAILED)


class RespuestasQueDejanDuda(Base):
    def test_code_22_compania_con_fallas_va_a_revision(self) -> None:
        proveedor = _proveedor(
            _manejador(
                httpx.Response(
                    200,
                    json={"code": 22, "message": "La compania Telcel esta presentando fallas"},
                )
            )
        )
        with self.assertRaises(ProviderIndeterminateError):
            proveedor.send_topup(_peticion())

    def test_code_24_duplicada_no_se_cierra_ni_se_desbloquea(self) -> None:
        """Puede que la anterior si se aplicara. Hay que averiguarlo.

        Y sobre todo: NO se llama a /sale/unlock. Automatizar ese endpoint
        despues de un duplicado es la receta exacta para la segunda recarga.
        """
        rutas: list[str] = []
        proveedor = _proveedor(
            _manejador(
                httpx.Response(
                    200,
                    json={
                        "code": 24,
                        "message": "Recarga duplicada del dia de hoy con telefono ...",
                    },
                ),
                rutas=rutas,
            )
        )
        with self.assertRaises(ProviderIndeterminateError) as caja:
            proveedor.send_topup(_peticion())
        self.assertEqual(caja.exception.external_code, "24")
        self.assertFalse([r for r in rutas if "unlock" in r])

    def test_un_500_deja_la_recarga_en_duda(self) -> None:
        proveedor = _proveedor(
            _manejador(httpx.Response(500, json={"code": 500, "message": "Error al realizar"}))
        )
        with self.assertRaises(ProviderIndeterminateError):
            proveedor.send_topup(_peticion())

    def test_un_503_deja_la_recarga_en_duda(self) -> None:
        proveedor = _proveedor(
            _manejador(
                httpx.Response(
                    503, json={"code": 503, "message": "Existe una transaccion en proceso"}
                )
            )
        )
        with self.assertRaises(ProviderIndeterminateError):
            proveedor.send_topup(_peticion())

    def test_un_timeout_de_lectura_deja_la_recarga_en_duda(self) -> None:
        proveedor = _proveedor(_manejador(_timeout_de_lectura))
        with self.assertRaises(ProviderIndeterminateError):
            proveedor.send_topup(_peticion())


class RechazosDeAutorizacion(Base):
    def test_un_401_en_compra_es_fallo_no_duda(self) -> None:
        """La capa de seguridad del proveedor corre ANTES de la logica de compra.

        Tratarlo como duda convertiria una caducidad de token rutinaria en
        una revision manual. El riesgo se acota renovando el token antes de
        comprar si le queda menos de un minuto de vida.
        """
        proveedor = _proveedor(
            _manejador(httpx.Response(401, json={"code": 401, "message": "sesion"}))
        )
        with self.assertRaises(ProviderPermanentError):
            proveedor.send_topup(_peticion())

    def test_un_403_geografico_se_reporta_como_tal(self) -> None:
        proveedor = _proveedor(
            _manejador(httpx.Response(403, json={"code": 403, "message": "Error country-US-403"}))
        )
        with self.assertRaises(ProviderPermanentError) as caja:
            proveedor.send_topup(_peticion())
        self.assertEqual(caja.exception.external_code, "PROVIDER_GEO_BLOCKED")


class ValidacionesAntesDeTocarLaRed(Base):
    def test_un_telefono_que_no_tiene_diez_digitos_no_se_manda(self) -> None:
        rutas: list[str] = []
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0, "authorization": "1"}), rutas=rutas)
        )
        malo = dataclasses.replace(_peticion(), phone_national="55123456")
        with self.assertRaises(ProviderPermanentError):
            proveedor.send_topup(malo)
        self.assertFalse([r for r in rutas if "purchase" in r])

    def test_un_product_id_que_no_es_idoffer_no_se_manda(self) -> None:
        rutas: list[str] = []
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0, "authorization": "1"}), rutas=rutas)
        )
        malo = dataclasses.replace(_peticion(), product_id="SL100")
        with self.assertRaises(ProviderPermanentError) as caja:
            proveedor.send_topup(malo)
        self.assertIn("mapping", caja.exception.message.lower())
        self.assertFalse([r for r in rutas if "purchase" in r])

    def test_sin_type_balance_no_se_manda(self) -> None:
        rutas: list[str] = []
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0, "authorization": "1"}), rutas=rutas),
            type_balance=None,
        )
        with self.assertRaises(ProviderNotConfigured):
            proveedor.send_topup(_peticion())
        self.assertFalse([r for r in rutas if "purchase" in r])

    def test_una_extra_comision_fuera_de_rango_no_se_manda(self) -> None:
        rutas: list[str] = []
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0, "authorization": "1"}), rutas=rutas),
            extra_comision=9,
        )
        with self.assertRaises(ProviderNotConfigured):
            proveedor.send_topup(_peticion())
        self.assertFalse([r for r in rutas if "purchase" in r])

    def test_un_proveedor_en_produccion_no_opera_en_la_suite(self) -> None:
        """La suite corre con ENVIRONMENT=test.

        Esto es lo que impide que una prueba gaste saldo real. Si se cae,
        alguien rompio la separacion de ambientes.
        """
        rutas: list[str] = []
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0, "authorization": "1"}), rutas=rutas),
            modo=ProviderMode.PRODUCTION,
            ambiente="production",
            base_url="https://api.linn.mx/api/v1/",
        )
        with self.assertRaises(ProviderNotConfigured):
            proveedor.send_topup(_peticion())
        self.assertFalse(rutas)

    def test_el_cuerpo_lleva_exactamente_los_cuatro_campos_documentados(self) -> None:
        cuerpos: list[bytes] = []

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return httpx.Response(200, json={"code": 0, "token": TOKEN_FALSO})
            if peticion.url.path.endswith("balance/getBalance"):
                return httpx.Response(200, json={"code": 0, "plataforma": "$5,000.00"})
            cuerpos.append(peticion.content)
            return httpx.Response(200, json={"code": 0, "authorization": "611701526"})

        proveedor = _proveedor(manejador)
        proveedor.send_topup(_peticion())

        import json as _json

        enviado = _json.loads(cuerpos[0])
        self.assertEqual(
            set(enviado), {"idOffer", "phoneNumber", "typeBalance", "extraComision"}
        )
        self.assertEqual(enviado["idOffer"], 96)
        self.assertEqual(enviado["phoneNumber"], TELEFONO)
        self.assertEqual(enviado["extraComision"], 0)


class GanchosDeConciliacionQueNoAplican(Base):
    def test_get_topup_status_nunca_devuelve_fallido(self) -> None:
        """Linntae no tiene consulta por autorizacion.

        Devolver FAILED desde un gancho que no puede preguntar es el error
        que ya costo dinero en este proyecto con Reloadly: leer "no existe"
        como "la recarga no se aplico" y reembolsar a quien si la recibio.
        """
        proveedor = _proveedor(_manejador(httpx.Response(200, json={"code": 0})))
        resultado = proveedor.get_topup_status("611701526")
        self.assertEqual(resultado.status, TopupStatus.UNKNOWN)
        self.assertNotEqual(resultado.status, TopupStatus.FAILED)

    def test_find_by_custom_identifier_dice_no_lo_se(self) -> None:
        proveedor = _proveedor(_manejador(httpx.Response(200, json={"code": 0})))
        self.assertIsNone(proveedor.find_by_custom_identifier("topup:lo-que-sea"))

    def test_estado_por_contexto_sin_credenciales_es_no_lo_se(self) -> None:
        proveedor = _proveedor(
            _manejador(httpx.Response(200, json={"code": 0})), username="", password=""
        )
        contexto = ContextoConciliacion(
            fulfillment_id=uuid.uuid4(),
            provider_product_id=ID_OFFER,
            phone_national=TELEFONO,
            amount=Money.parse("100", "MXN"),
            idempotency_key="topup:x",
        )
        self.assertIsNone(proveedor.estado_por_contexto(contexto))


class CatalogoTecnico(Base):
    def test_cada_oferta_se_convierte_en_un_producto_con_familia(self) -> None:
        """``raw["family"]`` es de donde el importador saca provider_family.

        Sin familia, el emparejamiento por identidad exacta no puede
        distinguir un saldo de un paquete del mismo precio, que es la trampa
        central de este dominio.
        """

        def manejador(peticion: httpx.Request) -> httpx.Response:
            if peticion.url.path.endswith("getToken"):
                return httpx.Response(200, json={"code": 0, "token": TOKEN_FALSO})
            if peticion.url.path.endswith("balance/getBalance"):
                return httpx.Response(200, json={"code": 0, "plataforma": "$5,000.00"})
            if peticion.url.path.endswith("config/syncProducts"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "products": [
                            {
                                "TIEMPO AIRE": [
                                    {
                                        "idOperator": 1,
                                        "name": "Telcel",
                                        "offers": [
                                            {
                                                "idOffer": 96,
                                                "amount": 10,
                                                "description": "1 DIA 50MB",
                                                "category": {"id": 1, "name": "Telefonia"},
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    },
                )
            return httpx.Response(404, json={"code": 404})

        proveedor = _proveedor(manejador)
        productos = proveedor.fetch_catalog()

        self.assertEqual(len(productos), 1)
        producto = productos[0]
        self.assertEqual(producto.provider_product_id, "96")
        self.assertEqual(producto.operator_name, "Telcel")
        self.assertEqual(producto.operator_code, "1")
        assert producto.amount is not None
        self.assertEqual(producto.amount.cents, 1_000)
        self.assertEqual(producto.raw["family"], "Telefonia")
        self.assertEqual(producto.raw["idOperator"], 1)
