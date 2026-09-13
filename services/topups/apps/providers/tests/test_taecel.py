"""Las tres llaves de TAECEL, y lo que pasa cuando falta una.

TAECEL tiene una particularidad que ningun otro proveedor del proyecto tiene:
su documentacion no es publica. Las rutas y los campos del adaptador provienen
de una fuente sin confirmar, no de su manual. Estas pruebas fijan la unica
conducta aceptable mientras eso siga siendo cierto: **no se toca la red**.

Ninguna prueba de este archivo hace una peticion real. Las que ejercitan el
transporte lo hacen contra un cliente falso, y hay una prueba dedicada a
verificar que sin las tres llaves el adaptador ni siquiera lo construye.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest import mock

import httpx
from django.test import SimpleTestCase

from apps.providers import taecel
from apps.providers.base import TopupRequest, TopupStatus
from samy_common.money import Money
from samy_common.providers.base import ProviderMode, ProviderStatus
from samy_common.providers.exceptions import (
    ProviderIndeterminateError,
    ProviderNotConfigured,
)

#: Credenciales de mentira para las pruebas. No son de nadie.
KEY_FALSA = "llave-de-prueba"
NIP_FALSO = "0000"
URL_FALSA = "https://ejemplo-invalido.test/ws"


def _proveedor(**kwargs: Any) -> taecel.TaecelProvider:
    base = {
        "base_url": "",
        "key": "",
        "nip": "",
        "contract_verified": False,
    }
    base.update(kwargs)
    return taecel.TaecelProvider(
        taecel.TaecelConfig(**base), ProviderMode.SANDBOX  # type: ignore[arg-type]
    )


def _peticion(*, amount_in_sku: bool = False) -> TopupRequest:
    return TopupRequest(
        fulfillment_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        operator_code="TELCEL",
        product_id="SL100",
        amount=Money.parse("100", "MXN"),
        phone_e164="+523121234567",
        phone_national="3121234567",
        phone_masked="312***4567",
        idempotency_key="prueba-" + uuid.uuid4().hex[:8],
        amount_in_sku=amount_in_sku,
    )


class TresLlaves(SimpleTestCase):
    """Falta una de las tres y TAECEL no opera. Sin excepciones."""

    def test_sin_credenciales_es_pending_contract(self) -> None:
        salud = _proveedor().check_health()
        self.assertIs(salud.status, ProviderStatus.PENDING_CONTRACT)
        self.assertIn("TAECEL_BASE_URL", salud.detail)

    def test_con_credenciales_pero_sin_contrato_confirmado_sigue_bloqueado(self) -> None:
        """El caso peligroso: hay llaves, asi que una llamada seria REAL.

        Si esta prueba se cae, significa que el adaptador esta dispuesto a
        mandar una recarga de verdad usando un contrato que nadie leyo.
        """
        salud = _proveedor(
            base_url=URL_FALSA, key=KEY_FALSA, nip=NIP_FALSO, contract_verified=False
        ).check_health()
        self.assertIs(salud.status, ProviderStatus.PENDING_CONTRACT)
        self.assertIn("TAECEL_CONTRACT_VERIFIED", salud.detail)

    def test_contrato_confirmado_sin_endpoint_de_saldo_no_vende(self) -> None:
        """TAECEL es prepago: sin saldo confirmado no se vende.

        DEGRADED, no READY. Vender sin saber si hay fondos puede dejar una
        orden ya pagada sin recarga, que es el peor fallo de este sistema.
        """
        salud = _proveedor(
            base_url=URL_FALSA, key=KEY_FALSA, nip=NIP_FALSO, contract_verified=True
        ).check_health()
        self.assertIs(salud.status, ProviderStatus.DEGRADED)
        self.assertFalse(salud.is_operational)
        self.assertIn("TAECEL_PATH_BALANCE", salud.detail)


class NoTocaLaRed(SimpleTestCase):
    """Sin las tres llaves no se abre ni un socket."""

    def test_send_topup_levanta_antes_de_construir_el_cliente(self) -> None:
        proveedor = _proveedor(base_url=URL_FALSA, key=KEY_FALSA, nip=NIP_FALSO)
        with mock.patch.object(
            taecel.TaecelProvider, "_client", side_effect=AssertionError("toco la red")
        ):
            with self.assertRaises(ProviderNotConfigured):
                proveedor.send_topup(_peticion())

    def test_get_topup_status_levanta_antes_de_construir_el_cliente(self) -> None:
        proveedor = _proveedor(base_url=URL_FALSA, key=KEY_FALSA, nip=NIP_FALSO)
        with mock.patch.object(
            taecel.TaecelProvider, "_client", side_effect=AssertionError("toco la red")
        ):
            with self.assertRaises(ProviderNotConfigured):
                proveedor.get_topup_status("12345")

    def test_fetch_catalog_levanta_antes_de_construir_el_cliente(self) -> None:
        proveedor = _proveedor(base_url=URL_FALSA, key=KEY_FALSA, nip=NIP_FALSO)
        with mock.patch.object(
            taecel.TaecelProvider, "_client", side_effect=AssertionError("toco la red")
        ):
            with self.assertRaises(ProviderNotConfigured):
                proveedor.fetch_catalog()


# ---------------------------------------------------------------------------
# Transporte simulado
# ---------------------------------------------------------------------------
# Con las tres llaves puestas el adaptador SI llama. Estas pruebas lo dejan
# llamar, pero contra un transporte falso de httpx: se ejercita el codigo real
# de armado de peticion y lectura de respuesta sin que salga un solo paquete.

RUTA_SALDO = "Saldo"
SALDO_SUFICIENTE = {"success": True, "saldo": "1,250.00"}


def _operativo(**kwargs: Any) -> taecel.TaecelProvider:
    """Proveedor con las tres llaves y endpoint de saldo. Puede operar."""
    base = {
        "base_url": URL_FALSA,
        "key": KEY_FALSA,
        "nip": NIP_FALSO,
        "contract_verified": True,
        "path_balance": RUTA_SALDO,
    }
    base.update(kwargs)
    return _proveedor(**base)


class TransporteFalso:
    """Enruta por ruta y guarda lo que se envio, para poder inspeccionarlo."""

    def __init__(self, respuestas: dict[str, Any]) -> None:
        self.respuestas = respuestas
        self.enviado: list[tuple[str, bytes]] = []

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        ruta = peticion.url.path.rsplit("/", 1)[-1]
        self.enviado.append((ruta, peticion.content))
        valor = self.respuestas.get(ruta, SALDO_SUFICIENTE)
        if isinstance(valor, Exception):
            raise valor
        if isinstance(valor, int):
            return httpx.Response(valor, json={"success": False})
        return httpx.Response(200, json=valor)


def _con_transporte(
    proveedor: taecel.TaecelProvider, transporte: TransporteFalso
) -> mock._patch[Any]:
    def fabricar() -> httpx.Client:
        return httpx.Client(
            base_url=URL_FALSA, transport=httpx.MockTransport(transporte)
        )

    return mock.patch.object(type(proveedor), "_client", side_effect=fabricar)


class LecturaDeEstado(SimpleTestCase):
    """Lo que no se reconoce va a conciliacion. Nunca se da por bueno."""

    def test_estado_desconocido_es_unknown_no_exito(self) -> None:
        proveedor = _proveedor()
        self.assertEqual(
            proveedor._leer_estado({"success": "algo-que-nadie-documento"}),
            TopupStatus.UNKNOWN,
        )

    def test_respuesta_vacia_es_unknown(self) -> None:
        self.assertEqual(_proveedor()._leer_estado({}), TopupStatus.UNKNOWN)

    def test_booleano_verdadero_es_exito(self) -> None:
        self.assertEqual(
            _proveedor()._leer_estado({"success": True}), TopupStatus.SUCCEEDED
        )

    def test_estado_anidado_en_data_se_encuentra(self) -> None:
        """No sabemos si responde plano o envuelto. Se mira en los dos sitios."""
        self.assertEqual(
            _proveedor()._leer_estado({"data": {"estado": "EXITOSA"}}),
            TopupStatus.SUCCEEDED,
        )

    def test_saldo_con_separadores_de_miles_se_lee(self) -> None:
        self.assertEqual(_proveedor()._leer_saldo({"saldo": "$1,250.00"}), 1250.0)

    def test_saldo_ilegible_es_none_no_cero(self) -> None:
        """``None`` es "no lo se". Cero seria afirmar que no hay saldo."""
        self.assertIsNone(_proveedor()._leer_saldo({"saldo": "mil pesos"}))


class EnvioDeRecarga(SimpleTestCase):
    """Lo que se manda, y lo que se concluye de lo que vuelve."""

    def test_saldo_suficiente_deja_el_proveedor_ready(self) -> None:
        proveedor = _operativo()
        transporte = TransporteFalso({RUTA_SALDO: SALDO_SUFICIENTE})
        with _con_transporte(proveedor, transporte):
            salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.READY)

    def test_saldo_en_cero_no_vende(self) -> None:
        proveedor = _operativo()
        transporte = TransporteFalso({RUTA_SALDO: {"success": True, "saldo": "0.00"}})
        with _con_transporte(proveedor, transporte):
            salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.DEGRADED)

    def test_saldo_irreconocible_no_vende(self) -> None:
        """Respondio, pero no se entendio. No se afirma READY sobre eso."""
        proveedor = _operativo()
        transporte = TransporteFalso({RUTA_SALDO: {"success": True, "otra_cosa": 1}})
        with _con_transporte(proveedor, transporte):
            salud = proveedor.check_health()
        self.assertIs(salud.status, ProviderStatus.DEGRADED)

    def test_exito_sin_folio_es_indeterminado_no_exito(self) -> None:
        """Sin folio no se puede verificar despues, asi que no se afirma nada.

        Es el caso que separa "parece que si" de "consta que si".
        """
        proveedor = _operativo()
        transporte = TransporteFalso(
            {RUTA_SALDO: SALDO_SUFICIENTE, "RequestTXN": {"success": True}}
        )
        with _con_transporte(proveedor, transporte):
            with self.assertRaises(ProviderIndeterminateError):
                proveedor.send_topup(_peticion())

    def test_exito_con_folio_es_exito(self) -> None:
        proveedor = _operativo()
        transporte = TransporteFalso(
            {
                RUTA_SALDO: SALDO_SUFICIENTE,
                "RequestTXN": {"success": True, "transID": "TX-777"},
            }
        )
        with _con_transporte(proveedor, transporte):
            resultado = proveedor.send_topup(_peticion())
        self.assertEqual(resultado.status, TopupStatus.SUCCEEDED)
        self.assertEqual(resultado.provider_reference, "TX-777")
        self.assertEqual(resultado.provider_mode, "SANDBOX")

    def test_timeout_es_indeterminado_jamas_fallo(self) -> None:
        """Un timeout no significa que no se aplico. Significa que no se sabe.

        Tratarlo como fallo llevaria a reembolsar una recarga que si llego, o
        peor, a reintentarla y aplicarla dos veces.
        """
        proveedor = _operativo()
        transporte = TransporteFalso(
            {
                RUTA_SALDO: SALDO_SUFICIENTE,
                "RequestTXN": httpx.ReadTimeout("se acabo el tiempo"),
            }
        )
        with _con_transporte(proveedor, transporte):
            with self.assertRaises(ProviderIndeterminateError):
                proveedor.send_topup(_peticion())

    def test_estado_desconocido_no_cierra_la_venta(self) -> None:
        proveedor = _operativo()
        transporte = TransporteFalso(
            {
                RUTA_SALDO: SALDO_SUFICIENTE,
                "RequestTXN": {"success": "quiza", "transID": "TX-9"},
            }
        )
        with _con_transporte(proveedor, transporte):
            resultado = proveedor.send_topup(_peticion())
        self.assertEqual(resultado.status, TopupStatus.UNKNOWN)
        self.assertFalse(resultado.succeeded)
        self.assertIsNone(resultado.delivered_amount)


class MontoCuandoCorresponde(SimpleTestCase):
    """El importe se manda solo si no viene ya dentro del SKU."""

    def _cuerpo_enviado(self, *, amount_in_sku: bool) -> str:
        proveedor = _operativo()
        transporte = TransporteFalso(
            {
                RUTA_SALDO: SALDO_SUFICIENTE,
                "RequestTXN": {"success": True, "transID": "TX-1"},
            }
        )
        with _con_transporte(proveedor, transporte):
            proveedor.send_topup(_peticion(amount_in_sku=amount_in_sku))
        enviados = [c for ruta, c in transporte.enviado if ruta == "RequestTXN"]
        return enviados[0].decode()

    def test_sku_de_denominacion_fija_no_manda_monto(self) -> None:
        """Mandar SKU fijo y monto a la vez es como se entrega otra cosa.

        Hay proveedores que al recibir los dos ignoran uno en silencio: se
        cobra $100 y se entrega el paquete de $200, o al contrario.
        """
        self.assertNotIn("Monto", self._cuerpo_enviado(amount_in_sku=True))

    def test_monto_libre_si_manda_monto(self) -> None:
        self.assertIn("Monto", self._cuerpo_enviado(amount_in_sku=False))


class CredencialesFueraDeLosLogs(SimpleTestCase):
    """Key y NIP no se registran. Nunca.

    Un log con credenciales las filtra a donde vayan los logs, y los logs se
    copian, se mandan por correo y se pegan en reportes de error.
    """

    def test_el_log_de_peticion_no_lleva_key_ni_nip(self) -> None:
        proveedor = _operativo()
        transporte = TransporteFalso(
            {
                RUTA_SALDO: SALDO_SUFICIENTE,
                "RequestTXN": {"success": True, "transID": "TX-1"},
            }
        )
        with mock.patch.object(taecel.log, "info") as log_info:
            with _con_transporte(proveedor, transporte):
                proveedor.send_topup(_peticion())

        registrado = repr(log_info.call_args_list)
        self.assertNotIn(KEY_FALSA, registrado)
        self.assertNotIn(NIP_FALSO, registrado)

    def test_las_credenciales_si_viajan_en_la_peticion(self) -> None:
        """El contrapunto del test anterior: no registrarlas no es no mandarlas."""
        proveedor = _operativo()
        transporte = TransporteFalso({RUTA_SALDO: SALDO_SUFICIENTE})
        with _con_transporte(proveedor, transporte):
            proveedor.check_health()
        cuerpo = transporte.enviado[0][1].decode()
        self.assertIn("Key", cuerpo)
        self.assertIn("NIP", cuerpo)


class SinReintentoAutomatico(SimpleTestCase):
    """El adaptador no reintenta por su cuenta. Jamas."""

    def test_un_solo_envio_por_llamada(self) -> None:
        """Una recarga reintentada a ciegas se aplica dos veces y se paga dos."""
        proveedor = _operativo()
        transporte = TransporteFalso(
            {RUTA_SALDO: SALDO_SUFICIENTE, "RequestTXN": httpx.ReadTimeout("timeout")}
        )
        with _con_transporte(proveedor, transporte):
            with self.assertRaises(ProviderIndeterminateError):
                proveedor.send_topup(_peticion())

        intentos = [ruta for ruta, _ in transporte.enviado if ruta == "RequestTXN"]
        self.assertEqual(len(intentos), 1)

    def test_no_sabe_buscar_por_referencia_propia_y_lo_admite(self) -> None:
        """``None`` es "no lo se", que manda la recarga a revision manual.

        TAECEL no documenta busqueda por referencia nuestra. Inventar que si
        sabe llevaria a concluir "no existe" y recargar de nuevo.
        """
        self.assertIsNone(_operativo().find_by_custom_identifier("ref-123"))
