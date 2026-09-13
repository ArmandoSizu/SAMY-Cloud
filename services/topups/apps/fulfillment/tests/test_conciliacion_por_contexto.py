"""La tercera via de conciliacion: preguntar con los datos de la operacion.

POR QUE HACE FALTA UNA TERCERA
------------------------------

Las dos anteriores suponen que el proveedor sabe contestar a una de estas
preguntas:

    ¿como quedo la operacion con TU folio X?
    ¿como quedo la operacion con MI referencia Y?

Linntae no sabe contestar a ninguna. Sus dos consultas identifican una recarga
por ``idOffer`` + ``telefono``. Con solo esos dos ganchos, la unica
implementacion honesta de su adaptador es devolver "no lo se" siempre, y
entonces ninguna de sus recargas se concilia nunca: todas acaban en revision
manual, incluidas las que el proveedor podria haber resuelto en una llamada.

Estas pruebas fijan que la nueva via se intenta PRIMERO y que no cambia nada
para los adaptadores que ya conciliaban por folio.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Operator, TopupProduct
from apps.fulfillment.models import TopupFulfillment
from apps.fulfillment.tasks import reconcile_pending_topups
from apps.providers.base import TopupResult, TopupStatus
from samy_common.states import FulfillmentState

TIENDA = uuid.uuid4()
ORGANIZACION = uuid.uuid4()
CAJERO = uuid.uuid4()


class ConciliacionPorContexto(TestCase):
    def setUp(self) -> None:
        operador = Operator.objects.create(
            provider_slug="linntae",
            provider_operator_id="1",
            name="Telcel",
            slug="telcel-linntae",
        )
        self.producto = TopupProduct.objects.create(
            operator=operador,
            provider_slug="linntae",
            # Para Linntae el identificador de producto ES el idOffer.
            provider_product_id="96",
            label="Telcel $100",
            currency="MXN",
            amount_cents=10_000,
        )
        self.cumplimiento = TopupFulfillment.objects.create(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product=self.producto,
            operator_name="Telcel",
            product_label="Telcel $100",
            phone_e164="+523121234567",
            phone_masked="31****4567",
            currency="MXN",
            amount_cents=10_000,
            state=FulfillmentState.UNDER_REVIEW,
            provider_slug="linntae",
            idempotency_key="topup:clave-interna-linntae",
            order_id=uuid.uuid4(),
        )
        TopupFulfillment.objects.filter(pk=self.cumplimiento.pk).update(
            updated_at=timezone.now() - timezone.timedelta(minutes=10)
        )

    def _doble(self, **kwargs) -> mock.MagicMock:
        p = mock.MagicMock()
        p.slug = "linntae"
        p.display_name = "Linntae"
        p.mode = "SANDBOX"
        p.estado_por_contexto.return_value = None
        p.find_by_custom_identifier.return_value = None
        for nombre, valor in kwargs.items():
            getattr(p, nombre).return_value = valor
        return p

    def _conciliar(self, proveedor):
        with mock.patch(
            "apps.fulfillment.tasks.get_provider", return_value=proveedor
        ):
            return reconcile_pending_topups()

    def _releer(self) -> TopupFulfillment:
        return TopupFulfillment.objects.get(pk=self.cumplimiento.pk)

    # -- la via nueva ------------------------------------------------------

    def test_se_pregunta_con_los_datos_de_la_operacion(self) -> None:
        proveedor = self._doble()
        self._conciliar(proveedor)

        proveedor.estado_por_contexto.assert_called_once()
        contexto = proveedor.estado_por_contexto.call_args.args[0]
        self.assertEqual(contexto.provider_product_id, "96")
        # El telefono nacional, sin el +52: es lo que Linntae espera.
        self.assertEqual(contexto.phone_national, "3121234567")
        self.assertEqual(contexto.amount.cents, 10_000)
        self.assertEqual(contexto.idempotency_key, "topup:clave-interna-linntae")

    def test_va_primero_y_corta_las_otras_dos(self) -> None:
        """Un adaptador implementa esto cuando es su consulta fiable.

        Si despues de resolver por contexto se siguiera preguntando por
        folio, la respuesta menos informada podria sobreescribir a la mejor.
        """
        proveedor = self._doble(
            estado_por_contexto=TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="6785",
                provider_mode="SANDBOX",
                operator_reference="12311057912",
            )
        )
        resumen = self._conciliar(proveedor)

        self.assertEqual(resumen["resolved_ok"], 1)
        proveedor.get_topup_status.assert_not_called()
        proveedor.find_by_custom_identifier.assert_not_called()

        recargado = self._releer()
        self.assertEqual(recargado.state, FulfillmentState.SUCCEEDED)
        self.assertEqual(recargado.provider_reference, "6785")
        self.assertEqual(recargado.operator_reference, "12311057912")

    def test_un_resultado_fallido_por_contexto_cierra_como_fallida(self) -> None:
        proveedor = self._doble(
            estado_por_contexto=TopupResult(
                status=TopupStatus.FAILED,
                provider_reference="6785",
                provider_mode="SANDBOX",
                failure_reason="Linntae la registro como fallida.",
            )
        )
        resumen = self._conciliar(proveedor)
        self.assertEqual(resumen["resolved_failed"], 1)
        self.assertEqual(self._releer().state, FulfillmentState.FAILED)

    def test_si_dice_no_lo_se_se_intentan_las_otras_dos(self) -> None:
        proveedor = self._doble()
        self._conciliar(proveedor)
        # Sin folio del proveedor, la segunda pregunta es por nuestra clave.
        proveedor.find_by_custom_identifier.assert_called_once_with(
            "topup:clave-interna-linntae"
        )

    def test_un_unknown_por_contexto_no_cierra_nada(self) -> None:
        """Es lo que devuelve ``get_topup_status`` de Linntae.

        UNKNOWN no es FAILED. Si esta prueba se cae cerrando la recarga, el
        sistema estaria reembolsando por no poder preguntar.
        """
        proveedor = self._doble(
            estado_por_contexto=TopupResult(
                status=TopupStatus.UNKNOWN,
                provider_reference="",
                provider_mode="SANDBOX",
            )
        )
        resumen = self._conciliar(proveedor)
        self.assertEqual(resumen["still_unknown"], 1)
        self.assertEqual(self._releer().state, FulfillmentState.UNDER_REVIEW)

    def test_nunca_se_reenvia_la_recarga(self) -> None:
        proveedor = self._doble(
            estado_por_contexto=TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="6785",
                provider_mode="SANDBOX",
            )
        )
        self._conciliar(proveedor)
        proveedor.send_topup.assert_not_called()
