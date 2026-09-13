"""Conciliar es PREGUNTAR, nunca reenviar ni adivinar.

Una recarga queda sin desenlace cuando el proveedor no respondio a tiempo. En
ese momento hay tres posibilidades y solo el proveedor sabe cual: se aplico,
no se aplico, o sigue en curso. Conciliar es ir a preguntarselo.

Las tres reglas que fijan estas pruebas:

1. **Se pregunta al proveedor que la ejecuto, jamas a otro.** Preguntarle a un
   segundo proveedor daria "no existe", que aqui se traduciria a FALLIDA. Es
   el failover prohibido entrando por la puerta de atras.
2. **Nunca se reenvia.** Ni un solo ``send_topup`` en toda la conciliacion.
3. **"No lo se" no es "no existe".** Lo que no se puede resolver queda en
   revision para una persona.

La prueba que documenta un fallo real y corregido es
``SinFolioSeBuscaPorNuestraReferencia.test_sin_folio_no_se_marca_fallida``.
El codigo anterior pasaba NUESTRA clave a ``get_topup_status()``, que espera
la del proveedor; Reloadly respondia 404, su adaptador traducia 404 a FAILED
-con razon, si el folio es suyo- y el resultado era marcar como fallida una
recarga que pudo haberse aplicado. Y entonces se reembolsaba a un cliente que
si habia recibido su saldo.
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


def _producto() -> TopupProduct:
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


class BaseConciliacion(TestCase):
    """Una recarga en revision, enviada hace rato y sin desenlace."""

    def setUp(self) -> None:
        self.producto = _producto()
        # Se crea directo en la base: create_fulfillment comprueba saldo y
        # aqui lo que se ejercita es la conciliacion, no la guarda de saldo.
        self.cumplimiento = TopupFulfillment.objects.create(
            organization_id=ORGANIZACION,
            store_id=TIENDA,
            requested_by_id=CAJERO,
            product=self.producto,
            operator_name="Operador de prueba",
            product_label="Recarga $50.00 MXN",
            phone_e164="+523121234567",
            phone_masked="31****4567",
            currency="MXN",
            amount_cents=5000,
            state=FulfillmentState.UNDER_REVIEW,
            provider_slug="reloadly",
            idempotency_key="topup:nuestra-clave-interna",
            order_id=uuid.uuid4(),
        )
        # El barrido solo mira lo que lleva mas de 2 minutos quieto.
        TopupFulfillment.objects.filter(pk=self.cumplimiento.pk).update(
            updated_at=timezone.now() - timezone.timedelta(minutes=10)
        )

    def _doble(self, **kwargs) -> mock.MagicMock:
        p = mock.MagicMock()
        p.slug = "reloadly"
        p.display_name = "Reloadly"
        p.mode = "SANDBOX"
        for nombre, valor in kwargs.items():
            if isinstance(valor, Exception):
                getattr(p, nombre).side_effect = valor
            else:
                getattr(p, nombre).return_value = valor
        return p

    def _conciliar(self, proveedor):
        with mock.patch(
            "apps.fulfillment.tasks.get_provider", return_value=proveedor
        ):
            return reconcile_pending_topups()

    def _releer(self) -> TopupFulfillment:
        return TopupFulfillment.objects.get(pk=self.cumplimiento.pk)


class SinFolioSeBuscaPorNuestraReferencia(BaseConciliacion):
    """El caso indeterminado: el timeout ocurre ANTES de que llegue el folio."""

    def test_se_busca_por_nuestra_clave_no_por_un_folio_inexistente(self) -> None:
        proveedor = self._doble(find_by_custom_identifier=None)
        self._conciliar(proveedor)

        # Se pregunto por NUESTRA referencia...
        proveedor.find_by_custom_identifier.assert_called_once_with(
            "topup:nuestra-clave-interna"
        )
        # ...y NO se le paso nuestra clave al consultor de folios.
        proveedor.get_topup_status.assert_not_called()

    def test_sin_folio_no_se_marca_fallida(self) -> None:
        """El fallo real que esto corrige.

        El doble imita lo que hace el adaptador de Reloadly DE VERDAD:
        ``get_topup_status`` con un folio que no reconoce responde 404, y el
        adaptador lo traduce a FAILED. Con razon: si el folio es suyo y no lo
        conoce, la recarga no existe.

        Pero nuestra clave interna no es su folio. El codigo anterior se la
        pasaba a ese metodo, cosechaba el FAILED y cerraba la recarga como
        fallida -- disparando un reembolso a un cliente que quiza si recibio
        su saldo. Con esta prueba, ese camino vuelve a fallar si alguien
        reintroduce el atajo.
        """
        proveedor = self._doble(
            find_by_custom_identifier=None,
            get_topup_status=TopupResult(
                status=TopupStatus.FAILED,
                provider_reference="",
                provider_mode="SANDBOX",
                failure_reason="La recarga no existe en Reloadly.",
            ),
        )
        resultado = self._conciliar(proveedor)

        self.assertEqual(self._releer().state_enum, FulfillmentState.UNDER_REVIEW)
        self.assertEqual(resultado["still_unknown"], 1)
        self.assertEqual(resultado["resolved_failed"], 0)

    def test_si_el_proveedor_la_encuentra_aplicada_se_cierra_bien(self) -> None:
        proveedor = self._doble(
            find_by_custom_identifier=TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="TX-REAL-777",
                provider_mode="SANDBOX",
            )
        )
        resultado = self._conciliar(proveedor)

        releido = self._releer()
        self.assertEqual(releido.state_enum, FulfillmentState.SUCCEEDED)
        self.assertEqual(releido.provider_reference, "TX-REAL-777")
        self.assertEqual(resultado["resolved_ok"], 1)


class ConFolioSeConsultaElFolio(BaseConciliacion):
    def setUp(self) -> None:
        super().setUp()
        TopupFulfillment.objects.filter(pk=self.cumplimiento.pk).update(
            provider_reference="TX-DEL-PROVEEDOR"
        )

    def test_se_consulta_por_el_folio_del_proveedor(self) -> None:
        proveedor = self._doble(
            get_topup_status=TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference="TX-DEL-PROVEEDOR",
                provider_mode="SANDBOX",
            )
        )
        self._conciliar(proveedor)

        proveedor.get_topup_status.assert_called_once_with("TX-DEL-PROVEEDOR")
        proveedor.find_by_custom_identifier.assert_not_called()

    def test_un_fallo_confirmado_por_folio_si_cierra_como_fallida(self) -> None:
        """Aqui el 404 SI significa algo: el folio es suyo y no lo conoce."""
        proveedor = self._doble(
            get_topup_status=TopupResult(
                status=TopupStatus.FAILED,
                provider_reference="TX-DEL-PROVEEDOR",
                provider_mode="SANDBOX",
                failure_reason="El operador rechazo el numero.",
            )
        )
        resultado = self._conciliar(proveedor)

        self.assertEqual(self._releer().state_enum, FulfillmentState.FAILED)
        self.assertEqual(resultado["resolved_failed"], 1)

    def test_un_estado_desconocido_deja_la_recarga_en_revision(self) -> None:
        proveedor = self._doble(
            get_topup_status=TopupResult(
                status=TopupStatus.UNKNOWN,
                provider_reference="TX-DEL-PROVEEDOR",
                provider_mode="SANDBOX",
            )
        )
        resultado = self._conciliar(proveedor)

        self.assertEqual(self._releer().state_enum, FulfillmentState.UNDER_REVIEW)
        self.assertEqual(resultado["still_unknown"], 1)

    def test_pendiente_tampoco_cierra(self) -> None:
        """En curso no es un desenlace. Se vuelve a preguntar en el barrido siguiente."""
        proveedor = self._doble(
            get_topup_status=TopupResult(
                status=TopupStatus.PENDING,
                provider_reference="TX-DEL-PROVEEDOR",
                provider_mode="SANDBOX",
            )
        )
        resultado = self._conciliar(proveedor)
        self.assertEqual(self._releer().state_enum, FulfillmentState.UNDER_REVIEW)
        self.assertEqual(resultado["still_unknown"], 1)


class NuncaReenvia(BaseConciliacion):
    """La regla mas importante de todo el archivo."""

    def test_ninguna_rama_llama_a_send_topup(self) -> None:
        """Se recorren TODAS las respuestas posibles del proveedor."""
        respuestas = [
            TopupResult(
                status=TopupStatus.SUCCEEDED, provider_reference="A", provider_mode="SANDBOX"
            ),
            TopupResult(
                status=TopupStatus.FAILED, provider_reference="B", provider_mode="SANDBOX"
            ),
            TopupResult(
                status=TopupStatus.PENDING, provider_reference="C", provider_mode="SANDBOX"
            ),
            TopupResult(
                status=TopupStatus.UNKNOWN, provider_reference="D", provider_mode="SANDBOX"
            ),
        ]
        for respuesta in respuestas:
            with self.subTest(estado=respuesta.status):
                TopupFulfillment.objects.filter(pk=self.cumplimiento.pk).update(
                    state=FulfillmentState.UNDER_REVIEW,
                    provider_reference="TX-DEL-PROVEEDOR",
                    updated_at=timezone.now() - timezone.timedelta(minutes=10),
                )
                proveedor = self._doble(get_topup_status=respuesta)
                self._conciliar(proveedor)
                proveedor.send_topup.assert_not_called()

    def test_tampoco_reenvia_cuando_no_se_encuentra(self) -> None:
        proveedor = self._doble(find_by_custom_identifier=None)
        self._conciliar(proveedor)
        proveedor.send_topup.assert_not_called()


class SiempreElMismoProveedor(BaseConciliacion):
    """Conciliar contra otro proveedor es el failover prohibido."""

    def test_se_pide_el_proveedor_que_la_ejecuto(self) -> None:
        proveedor = self._doble(find_by_custom_identifier=None)
        with mock.patch(
            "apps.fulfillment.tasks.get_provider", return_value=proveedor
        ) as pedido:
            reconcile_pending_topups()

        # El slug guardado en la recarga, no el proveedor por omision.
        pedido.assert_called_once_with("reloadly")

    def test_si_ese_proveedor_no_existe_se_deja_en_revision(self) -> None:
        """No se cae al proveedor por omision para "salvar" la conciliacion."""
        from samy_common.providers.exceptions import ProviderNotFound

        with mock.patch(
            "apps.fulfillment.tasks.get_provider",
            side_effect=ProviderNotFound(provider="reloadly", message="no registrado"),
        ):
            resultado = reconcile_pending_topups()

        self.assertEqual(self._releer().state_enum, FulfillmentState.UNDER_REVIEW)
        self.assertEqual(resultado["still_unknown"], 1)
