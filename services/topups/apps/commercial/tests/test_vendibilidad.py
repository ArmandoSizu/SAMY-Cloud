"""La regla que decide si se puede cobrar.

Estas pruebas existen para una sola cosa: que nunca se cobre algo que despues
no se pueda entregar. Cada una fija uno de los caminos por los que ese fallo
podria colarse.

Ninguna llama a un proveedor de verdad. La salud de los proveedores se pasa
como parametro, asi que la suite no toca la red ni depende de que Reloadly
este arriba.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.commercial.models import (
    CatalogStatus,
    CommercialFamily,
    CommercialOperator,
    CommercialProduct,
    CommercialProductVersion,
    Environment,
    MappingStatus,
    ProviderProductMapping,
)
from apps.commercial.services import catalogo_comercial, disponibilidad

TODOS_LISTOS = frozenset({"reloadly", "taecel"})
NINGUNO_LISTO: frozenset[str] = frozenset()


class BaseCatalogo(TestCase):
    """Un producto oficial y verificado, con su mapping. El caso feliz."""

    def setUp(self) -> None:
        self.telcel = CommercialOperator.objects.create(
            code="TELCEL", name="Telcel", display_priority=10, is_primary=True
        )
        self.familia = CommercialFamily.objects.create(
            operator=self.telcel, code="AMIGO_SIN_LIMITE", name="Amigo Sin Limite"
        )
        self.producto = CommercialProduct.objects.create(
            operator=self.telcel,
            family=self.familia,
            commercial_name="Amigo Sin Limite 100",
            price_cents=10000,
            official_verified=True,
            official_source="https://www.telcel.com/",
            official_tariff_reference="1960586",
            verified_at=timezone.now(),
            status=CatalogStatus.AVAILABLE,
        )
        CommercialProductVersion.objects.create(
            product=self.producto,
            version=1,
            price_cents=10000,
            validity_days=15,
            data_mb=1536,
            is_current=True,
        )
        self.mapping = ProviderProductMapping.objects.create(
            product=self.producto,
            provider_slug="reloadly",
            provider_product_id="12345",
            environment=Environment.SANDBOX,
            enabled=True,
            status=MappingStatus.OK,
        )

    def _estado(self, **kwargs):
        kwargs.setdefault("listos", TODOS_LISTOS)
        kwargs.setdefault("ambiente", Environment.SANDBOX)
        self.producto.refresh_from_db()
        return disponibilidad(self.producto, **kwargs)


class ReglaDeVendibilidadTests(BaseCatalogo):
    """OFICIAL + MAPPING + PROVEEDOR + AMBIENTE = SELLABLE. Falta una, no se vende."""

    def test_el_caso_completo_si_se_puede_vender(self) -> None:
        estado = self._estado()
        self.assertTrue(estado.vendible)
        self.assertEqual(estado.estado, CatalogStatus.AVAILABLE)
        self.assertEqual(estado.mapping, self.mapping)

    def test_sin_mapping_no_se_cobra(self) -> None:
        """El producto es real y esta verificado. Da igual: nadie lo ejecuta."""
        self.mapping.delete()

        estado = self._estado()

        self.assertFalse(estado.vendible)
        self.assertEqual(estado.estado, CatalogStatus.PROVIDER_NOT_MAPPED)

    def test_mapping_deshabilitado_no_se_cobra(self) -> None:
        self.mapping.enabled = False
        self.mapping.save()

        self.assertEqual(self._estado().estado, CatalogStatus.PROVIDER_NOT_MAPPED)

    def test_mapping_en_revision_no_se_cobra(self) -> None:
        """Un mapping que cambio bajo nuestros pies no vende hasta que lo miren."""
        self.mapping.marcar_para_revision("El precio del proveedor cambio.")

        self.assertEqual(self._estado().estado, CatalogStatus.PROVIDER_NOT_MAPPED)

    def test_proveedor_caido_no_se_cobra(self) -> None:
        """Hay mapping, pero el proveedor no responde. No es lo mismo."""
        estado = self._estado(listos=NINGUNO_LISTO)

        self.assertFalse(estado.vendible)
        self.assertEqual(estado.estado, CatalogStatus.PROVIDER_OFFLINE)
        # El motivo nombra al proveedor: es informacion de administracion.
        self.assertIn("reloadly", estado.motivo)

    def test_mapping_de_otro_ambiente_no_sirve(self) -> None:
        """Un mapping de produccion no habilita una venta en sandbox."""
        self.mapping.environment = Environment.PRODUCTION
        self.mapping.save()

        self.assertEqual(
            self._estado(ambiente=Environment.SANDBOX).estado,
            CatalogStatus.PROVIDER_NOT_MAPPED,
        )

    def test_sin_verificar_no_se_cobra(self) -> None:
        """Aunque haya mapping y proveedor: no sabemos que estamos vendiendo."""
        self.producto.official_verified = False
        self.producto.save()

        estado = self._estado()

        self.assertFalse(estado.vendible)
        self.assertEqual(estado.estado, CatalogStatus.REVIEW_REQUIRED)

    def test_producto_apagado_no_se_cobra(self) -> None:
        self.producto.active = False
        self.producto.save()

        self.assertEqual(self._estado().estado, CatalogStatus.DISABLED)

    def test_producto_deshabilitado_no_se_cobra(self) -> None:
        self.producto.status = CatalogStatus.DISABLED
        self.producto.save()

        self.assertEqual(self._estado().estado, CatalogStatus.DISABLED)

    def test_producto_en_revision_no_se_cobra(self) -> None:
        self.producto.status = CatalogStatus.REVIEW_REQUIRED
        self.producto.save()

        self.assertEqual(self._estado().estado, CatalogStatus.REVIEW_REQUIRED)

    def test_producto_vencido_no_se_cobra(self) -> None:
        """Por fecha, sin que nadie lo haya marcado a mano."""
        self.producto.available_until = timezone.localdate() - timedelta(days=1)
        self.producto.save()

        self.assertEqual(self._estado().estado, CatalogStatus.EXPIRED)

    def test_sandbox_only_no_se_cobra(self) -> None:
        self.producto.status = CatalogStatus.SANDBOX_ONLY
        self.producto.save()

        self.assertEqual(self._estado().estado, CatalogStatus.SANDBOX_ONLY)

    def test_el_estado_vendible_no_se_guarda(self) -> None:
        """Nada en la base dice "vendible". Se calcula siempre.

        Es la prueba de que no puede quedar una fila diciendo AVAILABLE
        mientras el proveedor esta caido: el mismo producto, sin tocar la
        base, cambia de respuesta segun el estado real de los proveedores.
        """
        self.assertTrue(self._estado(listos=TODOS_LISTOS).vendible)
        self.assertFalse(self._estado(listos=NINGUNO_LISTO).vendible)

        campos = {f.name for f in CommercialProduct._meta.get_fields()}
        self.assertNotIn("is_sellable", campos)
        self.assertNotIn("sellable", campos)
