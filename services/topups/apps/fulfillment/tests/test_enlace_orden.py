"""Pruebas del enlace entre la recarga y la orden que la cobra.

Este archivo nace de un fallo real encontrado al recorrer el flujo con
credenciales de verdad: la orden sabia a que recarga correspondia, pero la
recarga NO sabia su orden. Como ``execute_topup`` exige una orden para poder
comprobar que el pago se confirmo, la primera recarga real se habria cobrado
y jamas se habria entregado.

Las pruebas de abajo son las que habrian detectado eso antes.
"""

from __future__ import annotations

import json
import uuid

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Operator, TopupProduct
from apps.fulfillment import services
from apps.fulfillment.models import TopupFulfillment
from samy_common.security.signing import sign_request

TIENDA = uuid.uuid4()
ORGANIZACION = uuid.uuid4()
CAJERO = uuid.uuid4()


def _firmar(metodo: str, ruta: str, cuerpo: dict) -> tuple[bytes, dict[str, str]]:
    """Firma una peticion como lo haria el Core al llamar a este servicio.

    Se firma de verdad en vez de saltarse el middleware: asi la prueba
    recorre tambien la autenticacion entre servicios. Un endpoint que
    funcione en pruebas pero rechace al Core en produccion no sirve de nada.
    """
    crudo = json.dumps(cuerpo).encode()
    cabeceras = sign_request(
        secret=settings.SERVICE_S2S_SECRET,
        method=metodo,
        path=ruta,
        service="core",
        body=crudo,
    )
    return crudo, cabeceras.as_dict()


def _producto() -> TopupProduct:
    operador = Operator.objects.create(
        provider_slug="reloadly",
        provider_operator_id="298",
        name="Telcel Mexico Retail",
        slug="telcel-mexico-retail",
    )
    return TopupProduct.objects.create(
        operator=operador,
        provider_slug="reloadly",
        provider_product_id="298:179.7",
        label="Recarga $179.70 MXN",
        currency="MXN",
        amount_cents=17970,
    )


class EnlaceConLaOrdenTests(TestCase):
    def setUp(self) -> None:
        self.producto = _producto()
        self.recarga = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw="3121458890",
        )
        self.url = reverse(
            "fulfillment:link_order", args=[self.recarga.id]
        )

    def _enlazar(self, order_id: uuid.UUID, store_id: uuid.UUID = TIENDA):
        """POST firmado al endpoint de enlace."""
        cuerpo = {"order_id": str(order_id), "store_id": str(store_id)}
        crudo, cabeceras = _firmar("POST", self.url, cuerpo)
        return self.client.post(
            self.url,
            data=crudo,
            content_type="application/json",
            headers=cabeceras,
        )

    def test_una_recarga_nace_sin_orden(self) -> None:
        """Es correcto: la orden todavia no existe cuando se crea la recarga."""
        self.assertIsNone(self.recarga.order_id)

    def test_sin_firma_de_servicio_no_se_entra(self) -> None:
        """El endpoint es interno: sin firma S2S, 401."""
        respuesta = self.client.post(
            self.url,
            data={"order_id": str(uuid.uuid4()), "store_id": str(TIENDA)},
            content_type="application/json",
        )

        self.assertEqual(respuesta.status_code, 401)
        self.recarga.refresh_from_db()
        self.assertIsNone(self.recarga.order_id)

    def test_el_enlace_guarda_la_orden(self) -> None:
        orden = uuid.uuid4()

        respuesta = self._enlazar(orden)

        self.assertEqual(respuesta.status_code, 200)
        self.recarga.refresh_from_db()
        self.assertEqual(self.recarga.order_id, orden)

    def test_repetir_el_mismo_enlace_no_falla(self) -> None:
        """Un reintento de la misma peticion tiene que ser inofensivo."""
        orden = uuid.uuid4()

        self._enlazar(orden)
        segunda = self._enlazar(orden)

        self.assertEqual(segunda.status_code, 200)
        self.recarga.refresh_from_db()
        self.assertEqual(self.recarga.order_id, orden)

    def test_no_se_puede_reapuntar_a_otra_orden(self) -> None:
        """La defensa contra pagar una recarga barata y entregar una cara."""
        primera = uuid.uuid4()
        self._enlazar(primera)

        respuesta = self._enlazar(uuid.uuid4())

        self.assertEqual(respuesta.status_code, 409)
        self.recarga.refresh_from_db()
        self.assertEqual(self.recarga.order_id, primera)

    def test_otra_tienda_no_puede_enlazar_esta_recarga(self) -> None:
        """404 y no 403: no se confirma siquiera que la recarga exista."""
        respuesta = self._enlazar(uuid.uuid4(), store_id=uuid.uuid4())

        self.assertEqual(respuesta.status_code, 404)
        self.recarga.refresh_from_db()
        self.assertIsNone(self.recarga.order_id)

    def test_una_recarga_enlazada_ya_puede_verificar_su_pago(self) -> None:
        """El enlace es lo que hace posible ejecutar la recarga.

        Sin order_id, execute_topup rechaza por no poder comprobar el pago.
        Con order_id, la comprobacion se hace y decide ella.
        """
        from unittest import mock

        from samy_common.providers.exceptions import ProviderPermanentError

        orden = uuid.uuid4()
        self._enlazar(orden)
        self.recarga.refresh_from_db()

        with mock.patch(
            "apps.fulfillment.services._order_is_paid", return_value=False
        ) as verificar:
            with self.assertRaises(ProviderPermanentError):
                services.execute_topup(fulfillment=self.recarga)

        # Lo importante: SE CONSULTO el pago, con la orden correcta. Antes del
        # enlace ni siquiera se llegaba a preguntar.
        verificar.assert_called_once_with(orden)

    def test_sin_enlace_ni_siquiera_se_pregunta_por_el_pago(self) -> None:
        from unittest import mock

        from samy_common.providers.exceptions import ProviderPermanentError

        with mock.patch(
            "apps.fulfillment.services._order_is_paid", return_value=True
        ) as verificar:
            with self.assertRaises(ProviderPermanentError):
                services.execute_topup(fulfillment=self.recarga)

        verificar.assert_not_called()
        self.assertEqual(TopupFulfillment.objects.get(pk=self.recarga.pk).attempts, 0)
