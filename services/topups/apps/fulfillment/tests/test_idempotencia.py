"""Una recarga se envia UNA vez. Da igual cuantas veces se pida.

Las tres amenazas concretas, y son distintas entre si:

**Worker reiniciado.** Celery reentrega la tarea tras un reinicio. El worker
nuevo vuelve a leer el cumplimiento de la base y lo encuentra ya enviado.

**Dos workers a la vez.** Los dos leen QUEUED antes de que ninguno escriba.
Este es el caso peligroso, porque la guardia de "si ya no esta en cola, no
hagas nada" NO lo cubre: cuando se evalua, los dos ven cola. Lo que lo cubre
es que ``SENT -> SENT`` no existe en la maquina de estados y que la transicion
toma ``select_for_update``. El segundo se estrella al intentar pasar a SENT, y
se estrella ANTES de llamar al proveedor.

**Reintento tras una respuesta indeterminada.** El peor de los tres, porque
aqui la recarga pudo haberse aplicado ya. Reintentar la duplicaria y la
pagaria dos veces.

Estas pruebas existen porque la proteccion del segundo caso es una AUSENCIA
-la de una arista en el grafo de estados- y las ausencias se borran sin que
nadie note que protegian algo. Si alguien agrega ``SENT -> SENT`` para
"arreglar" un reintento, aqui se entera.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.test import TestCase

from apps.catalog.models import Operator, TopupProduct
from apps.fulfillment import services
from apps.fulfillment.models import TopupFulfillment
from apps.providers.base import TopupResult, TopupStatus
from samy_common.states import (
    FULFILLMENT_TRANSITIONS,
    FulfillmentState,
    IllegalTransition,
)

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


def _doble(resultado: TopupResult | Exception) -> mock.MagicMock:
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


EXITO = TopupResult(
    status=TopupStatus.SUCCEEDED,
    provider_reference="TX-UNICA",
    provider_mode="SANDBOX",
)


class LaMaquinaDeEstadosEsLaGuardia(TestCase):
    """La proteccion vive en lo que NO se puede hacer."""

    def test_sent_a_sent_no_existe(self) -> None:
        """Si esta arista aparece, dos workers pueden recargar dos veces.

        Es una prueba sobre una ausencia, y por eso es explicita: nadie
        revisando un diff nota que se perdio una proteccion al *agregar* una
        linea.
        """
        self.assertNotIn(
            FulfillmentState.SENT, FULFILLMENT_TRANSITIONS[FulfillmentState.SENT]
        )

    def test_desde_sent_solo_se_puede_cerrar(self) -> None:
        self.assertEqual(
            FULFILLMENT_TRANSITIONS[FulfillmentState.SENT],
            frozenset(
                {
                    FulfillmentState.SUCCEEDED,
                    FulfillmentState.FAILED,
                    FulfillmentState.UNDER_REVIEW,
                }
            ),
        )

    def test_desde_succeeded_no_se_vuelve_a_enviar(self) -> None:
        """Lo unico que queda tras una recarga entregada es reversarla."""
        self.assertEqual(
            FULFILLMENT_TRANSITIONS[FulfillmentState.SUCCEEDED],
            frozenset({FulfillmentState.REVERSED}),
        )

    def test_desde_under_review_no_se_reenvia(self) -> None:
        """Una recarga indeterminada se resuelve consultando, no reenviando."""
        self.assertNotIn(
            FulfillmentState.SENT,
            FULFILLMENT_TRANSITIONS[FulfillmentState.UNDER_REVIEW],
        )


class UnaSolaRecargaPorCumplimiento(TestCase):
    """Las tres amenazas, ejercitadas sobre ``execute_topup`` de verdad."""

    def setUp(self) -> None:
        self.producto = _catalogo()
        self.cumplimiento = services.create_fulfillment(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product_id=self.producto.id,
            phone_raw=TELEFONO,
        )
        self.cumplimiento.order_id = uuid.uuid4()
        self.cumplimiento.save(update_fields=["order_id"])

    def _ejecutar(self, cumplimiento, proveedor):
        with mock.patch(
            "apps.fulfillment.services._order_is_paid", return_value=True
        ), mock.patch(
            "apps.fulfillment.services.get_provider", return_value=proveedor
        ):
            return services.execute_topup(fulfillment=cumplimiento)

    def test_worker_reiniciado_no_recarga_dos_veces(self) -> None:
        """El worker nuevo relee de la base y encuentra la recarga cerrada."""
        proveedor = _doble(EXITO)
        self._ejecutar(self.cumplimiento, proveedor)
        self.assertEqual(proveedor.send_topup.call_count, 1)

        # La reentrega de la tarea: se relee de la base, como hace Celery.
        releido = TopupFulfillment.objects.get(pk=self.cumplimiento.pk)
        self.assertEqual(releido.state_enum, FulfillmentState.SUCCEEDED)
        self._ejecutar(releido, proveedor)

        self.assertEqual(proveedor.send_topup.call_count, 1)

    def test_dos_workers_con_el_mismo_estado_leido_solo_uno_envia(self) -> None:
        """El caso que la guardia de estado NO cubre.

        Se simula con dos objetos en memoria leidos AMBOS en QUEUED, que es
        exactamente lo que ven dos workers concurrentes. El primero envia y
        pasa a SENT; el segundo sigue creyendo que esta en cola, y aun asi no
        puede enviar, porque su transicion a SENT se valida contra el estado
        REAL de la base.
        """
        primero = TopupFulfillment.objects.get(pk=self.cumplimiento.pk)
        segundo = TopupFulfillment.objects.get(pk=self.cumplimiento.pk)
        self.assertEqual(primero.state_enum, segundo.state_enum)

        proveedor = _doble(EXITO)
        self._ejecutar(primero, proveedor)
        self.assertEqual(proveedor.send_topup.call_count, 1)

        with self.assertRaises(IllegalTransition):
            self._ejecutar(segundo, proveedor)

        # Lo unico que importa de esta prueba.
        self.assertEqual(proveedor.send_topup.call_count, 1)

    def test_tras_una_respuesta_indeterminada_no_se_reintenta(self) -> None:
        """El peor caso: la recarga pudo haberse aplicado.

        Reintentarla la duplicaria y la pagaria dos veces. Queda en revision y
        se resuelve consultando el estado real, nunca reenviando.
        """
        from samy_common.providers.exceptions import ProviderIndeterminateError

        proveedor = _doble(
            ProviderIndeterminateError(
                provider="reloadly", message="sin respuesta a tiempo"
            )
        )
        with self.assertRaises(ProviderIndeterminateError):
            self._ejecutar(self.cumplimiento, proveedor)

        releido = TopupFulfillment.objects.get(pk=self.cumplimiento.pk)
        self.assertEqual(releido.state_enum, FulfillmentState.UNDER_REVIEW)

        # Un segundo intento no vuelve a enviar nada.
        antes = proveedor.send_topup.call_count
        self._ejecutar(releido, proveedor)
        self.assertEqual(proveedor.send_topup.call_count, antes)

    def test_el_intento_se_cuenta_una_sola_vez(self) -> None:
        """``attempts`` es lo que delataria un doble envio en produccion."""
        proveedor = _doble(EXITO)
        self._ejecutar(self.cumplimiento, proveedor)
        releido = TopupFulfillment.objects.get(pk=self.cumplimiento.pk)
        self.assertEqual(releido.attempts, 1)

        self._ejecutar(releido, proveedor)
        releido.refresh_from_db()
        self.assertEqual(releido.attempts, 1)
