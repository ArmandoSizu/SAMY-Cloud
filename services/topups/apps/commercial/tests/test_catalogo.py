"""El catalogo como lo ve el cajero, y lo que NUNCA debe salir de ahi."""

from __future__ import annotations

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
from apps.commercial.router import (
    CambioDeProveedorProhibido,
    SinProveedorDisponible,
    ruta_para_reintento,
)
from apps.commercial.services import catalogo_comercial

LISTOS = frozenset({"reloadly"})


def _producto(operador, familia, nombre, centavos, **extra):
    p = CommercialProduct.objects.create(
        operator=operador,
        family=familia,
        commercial_name=nombre,
        price_cents=centavos,
        official_verified=extra.pop("official_verified", True),
        verified_at=timezone.now(),
        status=extra.pop("status", CatalogStatus.AVAILABLE),
        **extra,
    )
    CommercialProductVersion.objects.create(
        product=p, version=1, price_cents=centavos, is_current=True
    )
    return p


class OrdenComercialTests(TestCase):
    """Telcel primero, y por prioridad, no por accidente."""

    def setUp(self) -> None:
        datos = [("TELCEL", "Telcel", 10), ("MOVISTAR", "Movistar", 20),
                 ("ATT", "AT&T", 30), ("UNEFON", "Unefon", 40)]
        # Se crean al reves para que un orden correcto no pueda ser
        # casualidad del orden de insercion.
        self.operadores = {}
        for code, name, prioridad in reversed(datos):
            op = CommercialOperator.objects.create(
                code=code, name=name, display_priority=prioridad,
                is_primary=(code == "TELCEL"),
            )
            fam = CommercialFamily.objects.create(
                operator=op, code="RECARGA", name="Recarga"
            )
            _producto(op, fam, f"Recarga {name} 100", 10000)
            self.operadores[code] = op

    def test_telcel_aparece_primero(self) -> None:
        filas = catalogo_comercial(listos=LISTOS, ambiente=Environment.SANDBOX)
        self.assertEqual(filas[0]["operator_code"], "TELCEL")
        self.assertTrue(filas[0]["operator_is_primary"])

    def test_el_orden_completo_es_el_comercial(self) -> None:
        filas = catalogo_comercial(listos=LISTOS, ambiente=Environment.SANDBOX)
        orden = [f["operator_code"] for f in filas]
        self.assertEqual(orden, ["TELCEL", "MOVISTAR", "ATT", "UNEFON"])

    def test_los_cuatro_operadores_funcionan(self) -> None:
        filas = catalogo_comercial(listos=LISTOS, ambiente=Environment.SANDBOX)
        for code in ("TELCEL", "MOVISTAR", "ATT", "UNEFON"):
            with self.subTest(operador=code):
                self.assertTrue(any(f["operator_code"] == code for f in filas))


class LoQueElCajeroNoVeTests(TestCase):
    """El catalogo de caja no puede filtrar nada del proveedor."""

    def setUp(self) -> None:
        op = CommercialOperator.objects.create(code="TELCEL", name="Telcel")
        fam = CommercialFamily.objects.create(operator=op, code="SALDO", name="Saldo")
        self.producto = _producto(op, fam, "Recarga Amigo 100", 10000)
        ProviderProductMapping.objects.create(
            product=self.producto,
            provider_slug="reloadly",
            provider_product_id="SECRETO-123",
            environment=Environment.SANDBOX,
            enabled=True,
            status=MappingStatus.OK,
        )
        self.tecnico = _producto(
            op, fam, "Recarga $89.85 MXN", 8985, status=CatalogStatus.SANDBOX_ONLY
        )

    def test_no_se_filtra_ningun_dato_del_proveedor(self) -> None:
        """Ni el nombre, ni el identificador, ni el ambiente.

        Se comprueba sobre el texto entero de la respuesta y no campo por
        campo: si manana alguien anade una clave nueva con el slug dentro,
        esta prueba lo atrapa igual.
        """
        filas = catalogo_comercial(listos=LISTOS, ambiente=Environment.SANDBOX)
        texto = repr(filas).lower()

        for prohibido in ("reloadly", "taecel", "secreto-123", "provider_product_id",
                          "customidentifier", "wallet"):
            with self.subTest(dato=prohibido):
                self.assertNotIn(prohibido, texto)

    def test_los_productos_tecnicos_no_aparecen_en_caja(self) -> None:
        """Los $89.85 de sandbox existen, pero no en el mostrador."""
        filas = catalogo_comercial(listos=LISTOS, ambiente=Environment.SANDBOX)
        nombres = [f["commercial_name"] for f in filas]

        self.assertNotIn("Recarga $89.85 MXN", nombres)
        # Pero siguen en la base: son evidencia tecnica, no basura.
        self.assertTrue(
            CommercialProduct.objects.filter(status=CatalogStatus.SANDBOX_ONLY).exists()
        )

    def test_un_producto_bloqueado_aparece_pero_no_se_puede_cobrar(self) -> None:
        """Esconderlo haria pensar al cajero que no vendemos ese producto."""
        self.producto.mappings.all().delete()

        fila = next(
            f
            for f in catalogo_comercial(listos=LISTOS, ambiente=Environment.SANDBOX)
            if f["commercial_name"] == "Recarga Amigo 100"
        )

        self.assertFalse(fila["sellable"])
        self.assertEqual(fila["unavailable_reason"], "Temporalmente no disponible")
        # El motivo tecnico real NO llega a la caja.
        self.assertNotIn("mapping", fila["unavailable_reason"].lower())

    def test_el_precio_se_presenta_en_su_moneda(self) -> None:
        """Un producto en USD no puede mostrarse como si fueran pesos."""
        self.producto.currency = "USD"
        self.producto.save()

        fila = next(
            f
            for f in catalogo_comercial(listos=LISTOS, ambiente=Environment.SANDBOX)
            if f["commercial_name"] == "Recarga Amigo 100"
        )

        self.assertEqual(fila["currency"], "USD")
        self.assertIn("USD", fila["price_display"])
        self.assertNotIn("MXN", fila["price_display"])


class SinFailoverTests(TestCase):
    """Una recarga que ya salio NO se reencamina a otro proveedor.

    Es la regla mas importante del router. Un timeout no significa que la
    recarga no se aplico; mandarsela a otro proveedor "por si acaso" es como
    se recarga dos veces el mismo telefono y se cobra una.
    """

    def setUp(self) -> None:
        op = CommercialOperator.objects.create(code="TELCEL", name="Telcel")
        fam = CommercialFamily.objects.create(operator=op, code="SALDO", name="Saldo")
        self.producto = _producto(op, fam, "Recarga Amigo 100", 10000)
        # Dos proveedores podrian ejecutarlo. Ese es justo el escenario donde
        # un failover seria facil de escribir y caro de pagar.
        for slug, prioridad in (("reloadly", 10), ("taecel", 20)):
            ProviderProductMapping.objects.create(
                product=self.producto,
                provider_slug=slug,
                provider_product_id=f"{slug}-1",
                environment=Environment.SANDBOX,
                enabled=True,
                status=MappingStatus.OK,
                priority=prioridad,
            )

    def test_el_reintento_exige_el_proveedor_original(self) -> None:
        ruta = ruta_para_reintento(
            self.producto,
            provider_slug_original="reloadly",
            ambiente=Environment.SANDBOX,
        )
        self.assertEqual(ruta.provider_slug, "reloadly")

    def test_si_el_original_ya_no_sirve_NO_se_usa_el_otro(self) -> None:
        """Aunque haya un proveedor perfectamente disponible al lado."""
        self.producto.mappings.filter(provider_slug="reloadly").update(enabled=False)

        with self.assertRaises(CambioDeProveedorProhibido):
            ruta_para_reintento(
                self.producto,
                provider_slug_original="reloadly",
                ambiente=Environment.SANDBOX,
            )

    def test_el_router_no_expone_una_via_de_failover(self) -> None:
        """No existe funcion publica que devuelva "el siguiente proveedor"."""
        from apps.commercial import router

        publicas = {n for n in dir(router) if not n.startswith("_")}
        for sospechosa in ("failover", "siguiente_proveedor", "reintentar_con_otro"):
            self.assertNotIn(sospechosa, publicas)
