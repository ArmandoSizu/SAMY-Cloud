"""Identidad exacta, nunca precio.

El caso que estas pruebas existen para impedir es concreto y real: Telcel
vende con el mismo precio de $100 una recarga de saldo y un paquete Amigo Sin
Limite. Son productos distintos. Un emparejador que compare importes los
confunde, y entonces el cliente paga el paquete y recibe saldo, o al
contrario. El dinero ya se movio y el saldo del proveedor ya se gasto.

La prueba central de este archivo es
``SoloPrecio.test_mismo_importe_distinta_familia_no_empareja``.
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from apps.commercial.mapping import Rechazo, emparejar, emparejar_catalogo, guardar
from apps.commercial.models import (
    CatalogStatus,
    CommercialFamily,
    CommercialOperator,
    CommercialProduct,
    Environment,
    MappingStatus,
    ProviderCatalogItem,
)

PROVEEDOR = "taecel"


class BaseMapping(TestCase):
    """Telcel con dos familias al mismo precio. El escenario peligroso."""

    def setUp(self) -> None:
        self.telcel = CommercialOperator.objects.create(
            code="TELCEL",
            name="Telcel",
            display_priority=10,
            is_primary=True,
            provider_aliases={PROVEEDOR: ["TELCEL"]},
        )
        self.pasl = CommercialFamily.objects.create(
            operator=self.telcel,
            code="AMIGO_SIN_LIMITE",
            name="Amigo Sin Limite",
            display_priority=10,
            provider_aliases={PROVEEDOR: ["Amigo Sin Limite"]},
        )
        self.saldo = CommercialFamily.objects.create(
            operator=self.telcel,
            code="SALDO",
            name="Saldo",
            display_priority=20,
            provider_aliases={PROVEEDOR: ["Tiempo Aire"]},
        )

        # Dos productos NUESTROS, mismo precio, familias distintas.
        self.paquete_100 = CommercialProduct.objects.create(
            operator=self.telcel,
            family=self.pasl,
            commercial_name="Amigo Sin Limite 100",
            price_cents=10000,
            official_verified=True,
            official_source="https://www.telcel.com/",
            verified_at=timezone.now(),
            status=CatalogStatus.AVAILABLE,
        )
        self.saldo_100 = CommercialProduct.objects.create(
            operator=self.telcel,
            family=self.saldo,
            commercial_name="Saldo Telcel 100",
            price_cents=10000,
            official_verified=True,
            official_source="https://www.telcel.com/",
            verified_at=timezone.now(),
            status=CatalogStatus.AVAILABLE,
        )

    def _item(self, **kwargs: object) -> ProviderCatalogItem:
        datos: dict[str, object] = {
            "provider_slug": PROVEEDOR,
            "environment": Environment.SANDBOX,
            "provider_operator": "TELCEL",
            "amount_cents": 10000,
            "amount_in_sku": True,
        }
        datos.update(kwargs)
        item = ProviderCatalogItem(**datos)  # type: ignore[arg-type]
        item.fingerprint = item.calcular_fingerprint()
        item.save()
        return item


class SoloPrecio(BaseMapping):
    """La prueba que justifica todo el modulo."""

    def test_mismo_importe_distinta_familia_no_empareja(self) -> None:
        """El proveedor solo tiene el de SALDO. Nuestro producto es el PAQUETE.

        Coinciden operador e importe. Un emparejador por precio diria que si.
        Aqui tiene que decir que no, y decir por que.
        """
        self._item(
            provider_product_id="TAE100",
            provider_family="Tiempo Aire",
            provider_product_name="Tiempo Aire Telcel 100",
        )

        propuesta = emparejar(
            self.paquete_100,
            provider_slug=PROVEEDOR,
            environment=Environment.SANDBOX,
        )

        self.assertFalse(propuesta.hay_coincidencia)
        self.assertIs(propuesta.motivo, Rechazo.FAMILIA_NO_RECONOCIDA)
        # Y deja constancia de lo que un emparejador por precio habria elegido.
        self.assertEqual(len(propuesta.descartados_por_precio), 1)
        self.assertEqual(
            propuesta.descartados_por_precio[0].provider_product_id, "TAE100"
        )

    def test_cada_producto_va_a_su_propio_sku(self) -> None:
        """Con los dos SKUs presentes, cada uno cae donde debe."""
        self._item(
            provider_product_id="SL100",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
        )
        self._item(
            provider_product_id="TAE100",
            provider_family="Tiempo Aire",
            provider_product_name="Tiempo Aire Telcel 100",
        )

        paquete = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        saldo = emparejar(
            self.saldo_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )

        assert paquete.elegido is not None
        assert saldo.elegido is not None
        self.assertEqual(paquete.elegido.provider_product_id, "SL100")
        self.assertEqual(saldo.elegido.provider_product_id, "TAE100")


class CuatroPartesDeLaIdentidad(BaseMapping):
    """Falta una y no hay coincidencia. Cada fallo dice cual falto."""

    def test_sin_catalogo_importado_no_empareja(self) -> None:
        propuesta = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        self.assertIs(propuesta.motivo, Rechazo.SIN_CATALOGO)

    def test_operador_desconocido_pide_alias_de_operador(self) -> None:
        self._item(
            provider_product_id="SL100",
            provider_operator="TELCEL MEXICO SA",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
        )
        propuesta = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        self.assertIs(propuesta.motivo, Rechazo.OPERADOR_NO_RECONOCIDO)
        self.assertIn("provider_aliases", propuesta.detalle)

    def test_importe_distinto_no_empareja_aunque_todo_lo_demas_coincida(self) -> None:
        self._item(
            provider_product_id="SL200",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 200",
            amount_cents=20000,
        )
        propuesta = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        self.assertIs(propuesta.motivo, Rechazo.IMPORTE_NO_COINCIDE)
        self.assertIn("20000", propuesta.detalle)

    def test_sin_sku_no_empareja(self) -> None:
        self._item(
            provider_product_id="",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
        )
        propuesta = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        self.assertFalse(propuesta.hay_coincidencia)

    def test_dos_candidatos_identicos_es_ambiguo_no_el_primero(self) -> None:
        """Elegir al azar entre dos indistinguibles es el error a evitar."""
        self._item(
            provider_product_id="SL100-A",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
        )
        self._item(
            provider_product_id="SL100-B",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
        )
        propuesta = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        self.assertIs(propuesta.motivo, Rechazo.AMBIGUO)
        self.assertIn("SL100-A", propuesta.detalle)
        self.assertIn("SL100-B", propuesta.detalle)

    def test_ambiente_distinto_no_empareja(self) -> None:
        """Un SKU de sandbox no sirve en produccion. Son catalogos distintos."""
        self._item(
            provider_product_id="SL100",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
            environment=Environment.PRODUCTION,
        )
        propuesta = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        self.assertIs(propuesta.motivo, Rechazo.SIN_CATALOGO)


class GuardarNoAutoriza(BaseMapping):
    """Proponer no es aprobar. Ni con --guardar."""

    def setUp(self) -> None:
        super().setUp()
        self._item(
            provider_product_id="SL100",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
        )
        self.propuesta = emparejar(
            self.paquete_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )

    def test_el_mapping_nace_bloqueado(self) -> None:
        mapping = guardar(self.propuesta)
        assert mapping is not None
        self.assertFalse(mapping.enabled)
        self.assertEqual(mapping.status, MappingStatus.REVIEW_REQUIRED)
        self.assertFalse(mapping.es_utilizable)

    def test_el_mapping_guarda_las_cuatro_partes_para_poder_revisarlo(self) -> None:
        """Quien apruebe manana tiene que poder comparar sin abrir el portal."""
        mapping = guardar(self.propuesta)
        assert mapping is not None
        self.assertEqual(mapping.provider_product_id, "SL100")
        self.assertEqual(mapping.provider_family, "Amigo Sin Limite")
        self.assertEqual(mapping.provider_product_name, "Amigo Sin Limite 100")
        self.assertEqual(mapping.provider_amount_cents, 10000)
        self.assertTrue(mapping.identidad_completa)

    def test_el_producto_sigue_sin_ser_vendible(self) -> None:
        """El punto entero: emparejar no pone nada en la caja."""
        from apps.commercial.services import disponibilidad

        guardar(self.propuesta)
        estado = disponibilidad(
            self.paquete_100,
            listos=frozenset({PROVEEDOR}),
            ambiente=Environment.SANDBOX,
        )
        self.assertFalse(estado.vendible)
        self.assertIsNot(estado.estado, CatalogStatus.AVAILABLE)

    def test_sin_coincidencia_no_escribe_nada(self) -> None:
        vacia = emparejar(
            self.saldo_100, provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        self.assertIsNone(guardar(vacia))

    def test_identidad_incompleta_no_es_utilizable_aunque_este_aprobado(self) -> None:
        """Una fila editada a mano no se cuela por estar en OK y habilitada."""
        mapping = guardar(self.propuesta)
        assert mapping is not None
        mapping.enabled = True
        mapping.status = MappingStatus.OK
        mapping.provider_family = ""
        self.assertFalse(mapping.identidad_completa)
        self.assertFalse(mapping.es_utilizable)


class OrdenDeTrabajo(BaseMapping):
    """El reporte sale en el orden acordado: Telcel, Amigo Sin Limite, $100."""

    def test_telcel_amigo_sin_limite_cien_va_primero(self) -> None:
        CommercialProduct.objects.create(
            operator=self.telcel,
            family=self.pasl,
            commercial_name="Amigo Sin Limite 200",
            price_cents=20000,
            official_verified=True,
            official_source="https://www.telcel.com/",
            verified_at=timezone.now(),
            status=CatalogStatus.AVAILABLE,
        )
        self._item(
            provider_product_id="SL100",
            provider_family="Amigo Sin Limite",
            provider_product_name="Amigo Sin Limite 100",
        )

        propuestas = emparejar_catalogo(
            provider_slug=PROVEEDOR, environment=Environment.SANDBOX
        )
        nombres = [p.producto.commercial_name for p in propuestas]
        self.assertEqual(nombres[0], "Amigo Sin Limite 100")
        self.assertLess(
            nombres.index("Amigo Sin Limite 100"), nombres.index("Amigo Sin Limite 200")
        )
        self.assertLess(
            nombres.index("Amigo Sin Limite 100"), nombres.index("Saldo Telcel 100")
        )
