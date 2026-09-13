"""Conciliacion: averiguar que paso sin reintentar y sin adivinar.

La regla que estas pruebas defienden es una sola y vale dinero:

    Se cierra como exitosa o como fallida SOLO con un registro que lo diga.
    La AUSENCIA de registro no cierra nada.

Porque la ausencia tiene dos lecturas -la venta no existio, o el historico
todavia no la refleja- y la especificacion de Linntae no dice en cuanto tiempo
se indexa una venta. Elegir la primera lectura significa, cuando toca la
segunda, reembolsar a un cliente que si recibio su saldo.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

import httpx
from django.core.cache import cache
from django.test import SimpleTestCase
from django.utils import timezone

from apps.providers.base import TopupStatus
from apps.providers.linntae import conciliacion
from apps.providers.linntae.client import LinntaeClient, LinntaeConfig
from samy_common.money import Money

URL_DEMO_PRUEBA = "https://apidemo.linn.mx/api/v1/"
SECRETO_FALSO = "no-es-una-contrasena-real"
TOKEN_FALSO = "eyJwcmluY2lwYWwiOiJQUlVFQkEifQ"

TELEFONO = "3121234567"
ID_OFFER = "96"
MONTO = Money.parse("100", "MXN")


def _cliente(manejador: Callable[[httpx.Request], httpx.Response]) -> LinntaeClient:
    cliente = LinntaeClient(
        LinntaeConfig(
            base_url=URL_DEMO_PRUEBA,
            username="usuario-de-prueba",
            password=SECRETO_FALSO,
            ambiente="demo",
            type_balance=1,
            permitir_operaciones_reales=True,
            reintentos_lectura=0,
        )
    )
    cliente._cliente = httpx.Client(  # noqa: SLF001 - inyeccion deliberada
        base_url=URL_DEMO_PRUEBA,
        transport=httpx.MockTransport(manejador),
        headers={"Accept": "application/json"},
    )
    return cliente


def _doble(
    *,
    consulta: dict[str, Any] | None = None,
    ventas: list[dict[str, Any]] | None = None,
    rutas: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        ruta = peticion.url.path
        if rutas is not None:
            rutas.append(ruta)
        if ruta.endswith("getToken"):
            return httpx.Response(200, json={"code": 0, "token": TOKEN_FALSO})
        if ruta.endswith("sale/checkTransacctionTae"):
            return httpx.Response(200, json=consulta or {"code": 1, "message": "Producto invalido"})
        if ruta.endswith("sale/list"):
            return httpx.Response(200, json={"code": 0, "list": ventas or []})
        return httpx.Response(404, json={"code": 404, "message": ruta})

    return manejador


def _venta(
    *,
    exitosa: bool | None = True,
    monto: int = 100,
    referencia: str = TELEFONO,
    desplazamiento_min: int = 0,
    id_venta: int = 6785,
    folio: str = "12311057912",
) -> dict[str, Any]:
    momento = timezone.localtime(timezone.now()) + timedelta(minutes=desplazamiento_min)
    fila: dict[str, Any] = {
        "id": id_venta,
        "amount": monto,
        "totalAmount": monto,
        "reference": referencia,
        "productName": "TELCEL $100",
        "carrierName": "TELCEL",
        "folio": folio,
        "registerDate": momento.strftime("%d/%m/%Y %H:%M:%S"),
        "message": "Pago realizado correctamente",
    }
    if exitosa is not None:
        fila["successTransaction"] = exitosa
    return fila


def _contexto(*, edad_segundos: int = 10) -> conciliacion.ContextoRecarga:
    return conciliacion.contexto_desde_datos(
        id_offer=ID_OFFER,
        telefono_nacional=TELEFONO,
        monto=MONTO,
        enviado_en=timezone.now() - timedelta(seconds=edad_segundos),
        autorizacion="",
    )


class Base(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()


class VentanaDeLaConsultaDirecta(Base):
    def test_dentro_de_los_sesenta_segundos_se_pregunta_y_cierra(self) -> None:
        rutas: list[str] = []
        cliente = _cliente(
            _doble(
                consulta={"code": 0, "authorization": "611701526", "message": "ok"},
                rutas=rutas,
            )
        )
        resultado = conciliacion.resolver(
            cliente, _contexto(edad_segundos=10), modo="SANDBOX"
        )
        assert resultado is not None
        self.assertEqual(resultado.status, TopupStatus.SUCCEEDED)
        self.assertEqual(resultado.provider_reference, "611701526")
        self.assertTrue([r for r in rutas if "checkTransacctionTae" in r])

    def test_fuera_de_la_ventana_no_se_pregunta(self) -> None:
        """Su respuesta fuera de los 60 segundos no tiene significado definido.

        Y una respuesta que no se sabe interpretar no es informacion: es una
        forma de equivocarse con confianza.
        """
        rutas: list[str] = []
        cliente = _cliente(
            _doble(consulta={"code": 0, "authorization": "611701526"}, rutas=rutas)
        )
        conciliacion.resolver(cliente, _contexto(edad_segundos=600), modo="SANDBOX")
        self.assertFalse([r for r in rutas if "checkTransacctionTae" in r])
        self.assertTrue([r for r in rutas if "sale/list" in r])

    def test_sin_fecha_de_envio_tampoco_se_pregunta(self) -> None:
        contexto = conciliacion.contexto_desde_datos(
            id_offer=ID_OFFER, telefono_nacional=TELEFONO, monto=MONTO, enviado_en=None
        )
        self.assertFalse(contexto.admite_consulta_inmediata)


class ConsultaDirectaQueNoCierra(Base):
    def test_code_24_en_la_consulta_no_cierra_el_caso(self) -> None:
        """Dice que hay algo, no que fuera exitoso.

        El campo que lo afirma esta en el historico, asi que se sigue alli.
        """
        rutas: list[str] = []
        cliente = _cliente(
            _doble(
                consulta={"code": 24, "message": "Recarga duplicada del dia de hoy"},
                ventas=[_venta(exitosa=True)],
                rutas=rutas,
            )
        )
        resultado = conciliacion.resolver(cliente, _contexto(), modo="SANDBOX")
        assert resultado is not None
        self.assertEqual(resultado.status, TopupStatus.SUCCEEDED)
        self.assertTrue([r for r in rutas if "sale/list" in r])

    def test_code_cero_sin_autorizacion_no_cierra(self) -> None:
        cliente = _cliente(_doble(consulta={"code": 0}, ventas=[]))
        self.assertIsNone(conciliacion.resolver(cliente, _contexto(), modo="SANDBOX"))


class Historico(Base):
    def test_un_registro_exitoso_cierra_como_exitosa(self) -> None:
        cliente = _cliente(_doble(ventas=[_venta(exitosa=True)]))
        resultado = conciliacion.resolver(
            cliente, _contexto(edad_segundos=600), modo="SANDBOX"
        )
        assert resultado is not None
        self.assertEqual(resultado.status, TopupStatus.SUCCEEDED)
        # Se usa ``id``, no el folio: en el propio ejemplo de la
        # especificacion dos ventas distintas comparten folio.
        self.assertEqual(resultado.provider_reference, "6785")
        self.assertEqual(resultado.operator_reference, "12311057912")

    def test_un_registro_fallido_cierra_como_fallida(self) -> None:
        cliente = _cliente(_doble(ventas=[_venta(exitosa=False)]))
        resultado = conciliacion.resolver(
            cliente, _contexto(edad_segundos=600), modo="SANDBOX"
        )
        assert resultado is not None
        self.assertEqual(resultado.status, TopupStatus.FAILED)

    def test_un_registro_sin_veredicto_no_cierra(self) -> None:
        cliente = _cliente(_doble(ventas=[_venta(exitosa=None)]))
        self.assertIsNone(
            conciliacion.resolver(cliente, _contexto(edad_segundos=600), modo="SANDBOX")
        )

    def test_la_ausencia_de_registro_NO_cierra(self) -> None:
        """La prueba mas importante de este archivo.

        Si se cae devolviendo FAILED, el sistema reembolsaria recargas que
        quiza si se aplicaron, cada vez que el historico de Linntae tarde en
        reflejar una venta.
        """
        cliente = _cliente(_doble(ventas=[]))
        self.assertIsNone(
            conciliacion.resolver(cliente, _contexto(edad_segundos=600), modo="SANDBOX")
        )

    def test_dos_candidatas_indistinguibles_no_cierran(self) -> None:
        """Elegir una seria elegir al azar de que operacion es la evidencia."""
        cliente = _cliente(
            _doble(
                ventas=[
                    _venta(exitosa=True, id_venta=1),
                    _venta(exitosa=False, id_venta=2),
                ]
            )
        )
        self.assertIsNone(
            conciliacion.resolver(cliente, _contexto(edad_segundos=600), modo="SANDBOX")
        )


class Emparejamiento(Base):
    def test_otro_telefono_no_empareja(self) -> None:
        cliente = _cliente(_doble(ventas=[_venta(referencia="5512345678")]))
        self.assertIsNone(
            conciliacion.resolver(cliente, _contexto(edad_segundos=600), modo="SANDBOX")
        )

    def test_otro_monto_no_empareja(self) -> None:
        """Una recarga de $50 al mismo telefono no es esta recarga de $100."""
        cliente = _cliente(_doble(ventas=[_venta(monto=50)]))
        self.assertIsNone(
            conciliacion.resolver(cliente, _contexto(edad_segundos=600), modo="SANDBOX")
        )

    def test_fuera_de_la_ventana_temporal_no_empareja(self) -> None:
        """Sin esto, la recarga legitima de esta manana al mismo telefono por
        el mismo monto se emparejaria con la de esta tarde."""
        cliente = _cliente(_doble(ventas=[_venta(desplazamiento_min=120)]))
        self.assertIsNone(
            conciliacion.resolver(cliente, _contexto(edad_segundos=600), modo="SANDBOX")
        )

    def test_dentro_de_la_ventana_temporal_si_empareja(self) -> None:
        cliente = _cliente(_doble(ventas=[_venta(desplazamiento_min=-5)]))
        resultado = conciliacion.resolver(
            cliente, _contexto(edad_segundos=600), modo="SANDBOX"
        )
        assert resultado is not None
        self.assertEqual(resultado.status, TopupStatus.SUCCEEDED)

    def test_el_telefono_se_compara_por_digitos(self) -> None:
        """Linntae devuelve la referencia tal como se capturo."""
        cliente = _cliente(_doble(ventas=[_venta(referencia="312 123 4567")]))
        resultado = conciliacion.resolver(
            cliente, _contexto(edad_segundos=600), modo="SANDBOX"
        )
        assert resultado is not None
        self.assertEqual(resultado.status, TopupStatus.SUCCEEDED)


class NuncaSeDesbloquea(Base):
    def test_la_conciliacion_no_llama_a_sale_unlock(self) -> None:
        """Automatizar /sale/unlock produciria la segunda recarga.

        Sus bloqueos por venta reciente son una PROTECCION nuestra: 15
        minutos por debajo de $50 y 3 horas por encima. Quitarlos
        automaticamente despues de un resultado dudoso es exactamente como se
        recarga dos veces y se cobra una.
        """
        rutas: list[str] = []
        cliente = _cliente(
            _doble(
                consulta={"code": 24, "message": "duplicada"},
                ventas=[],
                rutas=rutas,
            )
        )
        conciliacion.resolver(cliente, _contexto(), modo="SANDBOX")
        self.assertFalse([r for r in rutas if "unlock" in r])
