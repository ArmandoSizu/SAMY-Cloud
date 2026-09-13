"""El saldo se comprueba antes de que exista la orden.

La secuencia que estas pruebas impiden:

    cobrar $100 -> orden PAID -> intentar recarga -> "saldo insuficiente"
    -> el cliente ya pago y no tiene recarga.

La guarda vive en ``create_fulfillment``, que corre ANTES de que exista la
orden. Por eso la prueba que mas importa aqui es
``test_no_se_crea_el_cumplimiento_y_por_tanto_no_hay_orden``: no comprueba un
mensaje, comprueba que no quedo NADA en la base que se pudiera cobrar.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.catalog.models import Operator, TopupProduct
from apps.fulfillment import services
from apps.fulfillment.models import TopupFulfillment
from apps.fulfillment.saldo import SaldoInsuficiente
from samy_common.money import Money
from samy_common.providers.base import ProviderMode
from samy_common.saldo import SALDO_NO_REPORTADO, SaldoProveedor

TIENDA = uuid.uuid4()
ORGANIZACION = uuid.uuid4()
CAJERO = uuid.uuid4()
TELEFONO = "3121234567"


def _catalogo() -> TopupProduct:
    operador = Operator.objects.create(
        provider_slug="reloadly",
        provider_operator_id="1234",
        name="Operador de prueba",
        slug="operador-de-prueba",
    )
    return TopupProduct.objects.create(
        operator=operador,
        provider_slug="reloadly",
        provider_product_id="1234:50",
        label="Recarga $50.00 MXN",
        currency="MXN",
        amount_cents=5000,
    )


def _proveedor(saldo: SaldoProveedor, modo: ProviderMode) -> mock.MagicMock:
    p = mock.MagicMock()
    p.slug = "reloadly"
    p.display_name = "Reloadly"
    p.mode = modo
    p.saldo_disponible.return_value = saldo
    return p


class GuardaDeSaldo(TestCase):
    def setUp(self) -> None:
        self.producto = _catalogo()
        cache.clear()  # el saldo se cachea por proveedor y modo

    def _crear(self, proveedor):
        with mock.patch(
            "apps.fulfillment.services.get_provider", return_value=proveedor
        ):
            return services.create_fulfillment(
                organization_id=ORGANIZACION,
                store_id=TIENDA,
                requested_by_id=CAJERO,
                product_id=self.producto.id,
                phone_raw=TELEFONO,
            )

    def test_no_se_crea_el_cumplimiento_y_por_tanto_no_hay_orden(self) -> None:
        """La prueba central: no queda nada que se pudiera cobrar."""
        proveedor = _proveedor(
            SaldoProveedor(disponible=Money(1_000, "MXN"), detalle="casi vacio"),
            ProviderMode.SANDBOX,
        )
        with self.assertRaises(SaldoInsuficiente):
            self._crear(proveedor)

        self.assertEqual(TopupFulfillment.objects.count(), 0)

    def test_con_saldo_suficiente_se_crea(self) -> None:
        proveedor = _proveedor(
            SaldoProveedor(disponible=Money(500_000, "MXN"), detalle="de sobra"),
            ProviderMode.SANDBOX,
        )
        cumplimiento = self._crear(proveedor)
        self.assertIsNotNone(cumplimiento.pk)

    def test_insuficiente_bloquea_tambien_en_sandbox(self) -> None:
        """Un saldo insuficiente confirmado no se salta ni en pruebas."""
        proveedor = _proveedor(
            SaldoProveedor(disponible=Money(0, "MXN"), detalle="vacio"),
            ProviderMode.SANDBOX,
        )
        with self.assertRaises(SaldoInsuficiente):
            self._crear(proveedor)

    def test_el_cajero_no_ve_el_saldo_del_proveedor(self) -> None:
        proveedor = _proveedor(
            SaldoProveedor(disponible=Money(1_000, "MXN"), detalle="casi vacio"),
            ProviderMode.SANDBOX,
        )
        with self.assertRaises(SaldoInsuficiente) as capturado:
            self._crear(proveedor)

        self.assertNotIn("10.00", capturado.exception.mensaje_caja)
        self.assertNotIn("reloadly", capturado.exception.mensaje_caja.lower())
        # El motivo tecnico si lo lleva, para el panel y los registros.
        self.assertIn("insuficiente", capturado.exception.motivo.lower())

    def test_sin_saldo_reportado_se_permite_en_sandbox(self) -> None:
        """Es el caso normal de una instalacion sin credenciales."""
        proveedor = _proveedor(SALDO_NO_REPORTADO, ProviderMode.SANDBOX)
        cumplimiento = self._crear(proveedor)
        self.assertIsNotNone(cumplimiento.pk)

    def test_sin_saldo_reportado_se_bloquea_en_produccion(self) -> None:
        """No se puede afirmar que podremos entregar."""
        proveedor = _proveedor(SALDO_NO_REPORTADO, ProviderMode.PRODUCTION)
        with self.assertRaises(SaldoInsuficiente):
            self._crear(proveedor)
        self.assertEqual(TopupFulfillment.objects.count(), 0)

    def test_monedero_en_otra_moneda_no_autoriza_en_produccion(self) -> None:
        """El monedero de Reloadly en USD contra una venta en MXN."""
        proveedor = _proveedor(
            SaldoProveedor(disponible=Money(1_000_000, "USD"), detalle="monedero USD"),
            ProviderMode.PRODUCTION,
        )
        with self.assertRaises(SaldoInsuficiente):
            self._crear(proveedor)

    @override_settings(TOPUP_RESERVA_SALDO_CENTS=100_000)
    def test_la_reserva_bloquea_un_saldo_que_alcanzaba(self) -> None:
        """El colchon evita que la venta siguiente falle a mitad."""
        proveedor = _proveedor(
            SaldoProveedor(disponible=Money(6_000, "MXN"), detalle="justo"),
            ProviderMode.SANDBOX,
        )
        with self.assertRaises(SaldoInsuficiente):
            self._crear(proveedor)

    def test_el_saldo_se_consulta_una_vez_por_racha(self) -> None:
        """Reloadly suspende cuentas por exceso de llamadas.

        Dos ventas seguidas no producen dos consultas: la segunda lee el
        saldo cacheado.
        """
        proveedor = _proveedor(
            SaldoProveedor(disponible=Money(500_000, "MXN"), detalle="de sobra"),
            ProviderMode.SANDBOX,
        )
        self._crear(proveedor)
        self._crear(proveedor)
        self.assertEqual(proveedor.saldo_disponible.call_count, 1)
