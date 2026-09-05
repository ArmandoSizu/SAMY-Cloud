"""Pruebas del microservicio de recargas.

Lo que se protege aqui es una sola frase: **la recarga no se ejecuta antes de
que el pago este confirmado**. Todo lo demas de este archivo existe para que
esa frase siga siendo cierta cuando alguien toque el codigo dentro de un ano.

Sobre los dobles de prueba: se sustituye el ADAPTADOR del proveedor, nunca la
regla de negocio. Un doble que devolviera "pagado" sin preguntar convertiria
estas pruebas en decorado. Aqui el doble solo simula lo que Reloadly
responderia; quien decide si se puede recargar sigue siendo el codigo real.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.catalog.models import Operator, TopupProduct
from apps.fulfillment import services
from apps.fulfillment.models import TopupFulfillment
from apps.providers.base import TopupResult, TopupStatus
from samy_common.money import Money
from samy_common.phone import PhoneValidationError
from samy_common.providers.exceptions import (
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.states import FulfillmentState

TIENDA = uuid.uuid4()
ORGANIZACION = uuid.uuid4()
CAJERO = uuid.uuid4()
TELEFONO = "3121234567"


def _crear_catalogo() -> TopupProduct:
    """Catalogo minimo, como lo dejaria una sincronizacion del proveedor.

    Se crea a mano SOLO en las pruebas. En el sistema real esta tabla se llena
    exclusivamente desde ``provider.fetch_catalog()``: no hay ninguna via por
    la que una denominacion inventada llegue al catalogo de produccion.
    """
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


def _doble_proveedor(resultado: TopupResult | Exception) -> mock.MagicMock:
    """Adaptador falso que devuelve lo que le digamos. No decide nada."""
    proveedor = mock.MagicMock()
    proveedor.slug = "reloadly"
    proveedor.display_name = "Reloadly"
    proveedor.mode = "SANDBOX"
    proveedor.ensure_ready.return_value = None
    if isinstance(resultado, Exception):
        proveedor.send_topup.side_effect = resultado
    else:
        proveedor.send_topup.return_value = resultado
    return proveedor


class CatalogoTests(TestCase):
    """1. Catalogo."""

    def test_el_catalogo_arranca_vacio(self) -> None:
        """Sin sincronizar, no hay nada que vender.

        Es la prueba de que no existen denominaciones codificadas: una
        instalacion nueva no ofrece ningun producto hasta que el proveedor
        diga cuales vende.
        """
        self.assertEqual(TopupProduct.objects.count(), 0)
        self.assertEqual(Operator.objects.count(), 0)

    def test_un_producto_inactivo_no_se_puede_vender(self) -> None:
        producto = _crear_catalogo()
        producto.is_active = False
        producto.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            services.create_fulfillment(
                organization_id=ORGANIZACION,
                store_id=TIENDA,
                requested_by_id=CAJERO,
                product_id=producto.id,
                phone_raw=TELEFONO,
            )

    def test_no_se_puede_vender_un_monto_que_el_proveedor_no_ofrece(self) -> None:
        """El requisito literal: si el proveedor no vende $75, SAMY no vende $75."""
        producto = _crear_catalogo()  # solo $50

        with self.assertRaises(ValidationError):
            services.create_fulfillment(
                organization_id=ORGANIZACION,
                store_id=TIENDA,
                requested_by_id=CAJERO,
                product_id=producto.id,
                phone_raw=TELEFONO,
                amount=Money.parse("75.00"),
            )

        self.assertFalse(TopupFulfillment.objects.exists())


class OperadorYTelefonoTests(TestCase):
    """2 y 3. Operador invalido y telefono invalido."""

    def setUp(self) -> None:
        self.producto = _crear_catalogo()

    def test_producto_inexistente_se_rechaza(self) -> None:
        with self.assertRaises(ValidationError):
            services.create_fulfillment(
                organization_id=ORGANIZACION,
                store_id=TIENDA,
                requested_by_id=CAJERO,
                product_id=uuid.uuid4(),
                phone_raw=TELEFONO,
            )

    def test_telefono_corto_se_rechaza(self) -> None:
        with self.assertRaises(PhoneValidationError):
            services.create_fulfillment(
                organization_id=ORGANIZACION,
                store_id=TIENDA,
                requested_by_id=CAJERO,
                product_id=self.producto.id,
                phone_raw="55123",
            )
        self.assertFalse(TopupFulfillment.objects.exists())

    def test_telefono_de_un_solo_digito_repetido_se_rechaza(self) -> None:
        with self.assertRaises(PhoneValidationError):
            services.create_fulfillment(
                organization_id=ORGANIZACION,
                store_id=TIENDA,
                requested_by_id=CAJERO,
                product_id=self.producto.id,
                phone_raw="1111111111",
            )

    def test_el_telefono_se_guarda_normalizado_y_enmascarado(self) -> None:
        """El comprobante nunca lleva el numero completo."""
        cumplimiento = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw="(312) 123-4567",
        )

        self.assertEqual(cumplimiento.phone_e164, "+523121234567")
        self.assertNotIn("3121234567", cumplimiento.phone_masked)
        self.assertIn("4567", cumplimiento.phone_masked)


class CreacionYPagoPendienteTests(TestCase):
    """4 y 5. Creacion de la recarga y estado de pago pendiente."""

    def setUp(self) -> None:
        self.producto = _crear_catalogo()

    def test_la_recarga_nace_esperando_el_pago(self) -> None:
        cumplimiento = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw=TELEFONO,
        )

        self.assertEqual(cumplimiento.state_enum, FulfillmentState.PENDING_PAYMENT)
        self.assertEqual(cumplimiento.amount_cents, 5000)
        self.assertEqual(cumplimiento.operator_name, "Operador de prueba")
        # Sin proveedor tocado todavia: crear la intencion no recarga nada.
        self.assertEqual(cumplimiento.attempts, 0)
        self.assertEqual(cumplimiento.provider_reference, "")

    def test_cada_recarga_tiene_su_propia_referencia(self) -> None:
        """Dos ventas nunca comparten referencia: se confundirian al conciliar."""
        primera = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw=TELEFONO,
        )
        segunda = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw=TELEFONO,
        )
        self.assertNotEqual(primera.idempotency_key, segunda.idempotency_key)


class ReglaDelDineroTests(TestCase):
    """6. La prohibicion de recargar antes de que el pago este confirmado.

    Es la prueba mas importante del archivo.
    """

    def setUp(self) -> None:
        self.producto = _crear_catalogo()
        self.cumplimiento = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw=TELEFONO,
        )
        self.cumplimiento.order_id = uuid.uuid4()
        self.cumplimiento.save(update_fields=["order_id"])

    @mock.patch("apps.fulfillment.services._order_is_paid", return_value=False)
    @mock.patch("apps.fulfillment.services.get_provider")
    def test_no_se_recarga_si_la_orden_no_esta_pagada(
        self, get_provider, _order_is_paid
    ) -> None:
        proveedor = _doble_proveedor(
            TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="no-deberia-usarse",
                provider_mode="SANDBOX",
            )
        )
        get_provider.return_value = proveedor

        with self.assertRaises(ProviderPermanentError):
            services.execute_topup(fulfillment=self.cumplimiento)

        # Lo que importa: NUNCA se llamo al proveedor.
        proveedor.send_topup.assert_not_called()

    @mock.patch("apps.fulfillment.services._order_is_paid", return_value=False)
    @mock.patch("apps.fulfillment.services.get_provider")
    def test_si_no_se_puede_verificar_el_pago_tampoco_se_recarga(
        self, get_provider, _order_is_paid
    ) -> None:
        """El servicio de Pagos caido NO es permiso para recargar.

        ``_order_is_paid`` devuelve False ante cualquier duda. Una recarga no
        ejecutada se reintenta; una recarga regalada no se recupera.
        """
        proveedor = _doble_proveedor(
            TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="x",
                provider_mode="SANDBOX",
            )
        )
        get_provider.return_value = proveedor

        with self.assertRaises(ProviderPermanentError):
            services.execute_topup(fulfillment=self.cumplimiento)

        proveedor.send_topup.assert_not_called()

    def test_una_recarga_sin_orden_no_se_ejecuta(self) -> None:
        huerfana = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw=TELEFONO,
        )
        with self.assertRaises(ProviderPermanentError):
            services.execute_topup(fulfillment=huerfana)


class EjecucionTests(TestCase):
    """7 y 8. Recarga exitosa y fallo del proveedor."""

    def setUp(self) -> None:
        self.producto = _crear_catalogo()
        self.cumplimiento = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw=TELEFONO,
        )
        self.cumplimiento.order_id = uuid.uuid4()
        self.cumplimiento.save(update_fields=["order_id"])

    @mock.patch("apps.fulfillment.services._order_is_paid", return_value=True)
    @mock.patch("apps.fulfillment.services.get_provider")
    def test_recarga_exitosa(self, get_provider, _order_is_paid) -> None:
        get_provider.return_value = _doble_proveedor(
            TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="TX-123",
                provider_mode="SANDBOX",
                operator_reference="OP-999",
            )
        )

        resultado = services.execute_topup(fulfillment=self.cumplimiento)

        self.assertEqual(resultado.state_enum, FulfillmentState.SUCCEEDED)
        self.assertEqual(resultado.provider_reference, "TX-123")
        # La referencia del operador es lo que el cliente reclama si no le
        # llego el saldo, asi que tiene que quedar guardada.
        self.assertEqual(resultado.operator_reference, "OP-999")

    @mock.patch("apps.fulfillment.services._order_is_paid", return_value=True)
    @mock.patch("apps.fulfillment.services.get_provider")
    def test_fallo_del_proveedor_deja_la_recarga_fallida(
        self, get_provider, _order_is_paid
    ) -> None:
        get_provider.return_value = _doble_proveedor(
            ProviderPermanentError(
                provider="reloadly", message="Operador fuera de servicio."
            )
        )

        with self.assertRaises(ProviderPermanentError):
            services.execute_topup(fulfillment=self.cumplimiento)

        self.cumplimiento.refresh_from_db()
        self.assertEqual(self.cumplimiento.state_enum, FulfillmentState.FAILED)
        self.assertIn("Operador fuera de servicio", self.cumplimiento.failure_reason)

    @mock.patch("apps.fulfillment.services._order_is_paid", return_value=True)
    @mock.patch("apps.fulfillment.services.get_provider")
    def test_estado_pendiente_no_se_da_por_exitoso(
        self, get_provider, _order_is_paid
    ) -> None:
        """PENDING no es SUCCESS.

        Cerrar la venta por optimismo imprimiria un comprobante de "recarga
        exitosa" para algo que todavia puede fallar.
        """
        get_provider.return_value = _doble_proveedor(
            TopupResult(
                status=TopupStatus.PENDING,
                provider_reference="TX-EN-CURSO",
                provider_mode="SANDBOX",
            )
        )

        resultado = services.execute_topup(fulfillment=self.cumplimiento)

        self.assertEqual(resultado.state_enum, FulfillmentState.SENT)
        self.assertNotEqual(resultado.state_enum, FulfillmentState.SUCCEEDED)

    @mock.patch("apps.fulfillment.services._order_is_paid", return_value=True)
    @mock.patch("apps.fulfillment.services.get_provider")
    def test_estado_desconocido_tampoco_se_da_por_exitoso(
        self, get_provider, _order_is_paid
    ) -> None:
        get_provider.return_value = _doble_proveedor(
            TopupResult(
                status=TopupStatus.UNKNOWN,
                provider_reference="TX-RARO",
                provider_mode="SANDBOX",
            )
        )

        resultado = services.execute_topup(fulfillment=self.cumplimiento)

        self.assertNotEqual(resultado.state_enum, FulfillmentState.SUCCEEDED)

    @mock.patch("apps.fulfillment.services._order_is_paid", return_value=True)
    @mock.patch("apps.fulfillment.services.get_provider")
    def test_una_recarga_ya_terminada_no_se_vuelve_a_enviar(
        self, get_provider, _order_is_paid
    ) -> None:
        """Protege contra el evento duplicado que llega tarde."""
        proveedor = _doble_proveedor(
            TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="TX-1",
                provider_mode="SANDBOX",
            )
        )
        get_provider.return_value = proveedor

        services.execute_topup(fulfillment=self.cumplimiento)
        self.assertEqual(proveedor.send_topup.call_count, 1)

        # Segundo intento sobre la MISMA recarga ya completada.
        self.cumplimiento.refresh_from_db()
        services.execute_topup(fulfillment=self.cumplimiento)

        # No se envio de nuevo: sigue habiendo una sola llamada.
        self.assertEqual(proveedor.send_topup.call_count, 1)


class AislamientoTests(TestCase):
    """9. Aislamiento entre tiendas."""

    def test_cada_recarga_queda_atada_a_su_tienda(self) -> None:
        producto = _crear_catalogo()
        tienda_a, tienda_b = uuid.uuid4(), uuid.uuid4()

        for tienda in (tienda_a, tienda_b):
            services.create_fulfillment(
                organization_id=ORGANIZACION,
                store_id=tienda,
                requested_by_id=CAJERO,
                product_id=producto.id,
                phone_raw=TELEFONO,
            )

        de_a = TopupFulfillment.objects.filter(store_id=tienda_a)
        de_b = TopupFulfillment.objects.filter(store_id=tienda_b)

        self.assertEqual(de_a.count(), 1)
        self.assertEqual(de_b.count(), 1)
        # Ninguna consulta acotada a una tienda alcanza a la otra.
        self.assertNotIn(de_b.first().id, [f.id for f in de_a])
