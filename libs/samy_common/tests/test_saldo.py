"""El saldo se comprueba antes de cobrar. Y no comparar monedas distintas.

La prueba que justifica la mitad del modulo es
``MonedaDistinta.test_no_se_comparan_monedas_distintas``. El monedero de
Reloadly esta en USD y las recargas se venden en MXN: comparar 100 USD con
100 MXN concluye "hay saldo" por razones equivocadas.
"""

from __future__ import annotations

import unittest

from samy_common.money import Money
from samy_common.saldo import (
    SALDO_NO_REPORTADO,
    SaldoProveedor,
    Suficiencia,
    bloquea,
    verificar,
)

CIEN_MXN = Money(10_000, "MXN")


def _saldo(cents: int, moneda: str = "MXN") -> SaldoProveedor:
    return SaldoProveedor(disponible=Money(cents, moneda), detalle="prueba")


class SaldoSuficiente(unittest.TestCase):
    def test_con_saldo_de_sobra_se_vende(self) -> None:
        v = verificar(_saldo(50_000), CIEN_MXN, ambiente_productivo=True)
        self.assertIs(v.suficiencia, Suficiencia.SUFICIENTE)
        self.assertTrue(v.permite_vender)
        self.assertFalse(bloquea(v, ambiente_productivo=True))

    def test_el_saldo_exacto_alcanza(self) -> None:
        """Sin reserva, gastar el ultimo centavo es legitimo."""
        v = verificar(_saldo(10_000), CIEN_MXN, ambiente_productivo=True)
        self.assertIs(v.suficiencia, Suficiencia.SUFICIENTE)

    def test_un_centavo_de_menos_no_alcanza(self) -> None:
        v = verificar(_saldo(9_999), CIEN_MXN, ambiente_productivo=True)
        self.assertIs(v.suficiencia, Suficiencia.INSUFICIENTE)


class SaldoInsuficienteBloqueaSiempre(unittest.TestCase):
    """La regla dura: no depende del ambiente."""

    def test_bloquea_en_produccion(self) -> None:
        v = verificar(_saldo(5_000), CIEN_MXN, ambiente_productivo=True)
        self.assertTrue(bloquea(v, ambiente_productivo=True))

    def test_bloquea_tambien_en_sandbox(self) -> None:
        """Un saldo insuficiente CONFIRMADO no se salta ni en pruebas.

        No es celo: si en sandbox se permitiera, el camino "cobrado y sin
        entregar" nunca se probaria y llegaria virgen a produccion.
        """
        v = verificar(_saldo(5_000), CIEN_MXN, ambiente_productivo=False)
        self.assertTrue(bloquea(v, ambiente_productivo=False))
        self.assertTrue(v.es_bloqueo_duro)

    def test_el_motivo_trae_los_dos_numeros(self) -> None:
        """Quien lo lea a las once de la noche necesita saber cuanto falta."""
        v = verificar(_saldo(5_000), CIEN_MXN, ambiente_productivo=True)
        self.assertIn("50.00", v.motivo)
        self.assertIn("100.00", v.motivo)

    def test_el_cajero_no_ve_el_saldo_del_proveedor(self) -> None:
        v = verificar(_saldo(5_000), CIEN_MXN, ambiente_productivo=True)
        self.assertNotIn("50.00", v.mensaje_caja)
        self.assertNotIn("saldo", v.mensaje_caja.lower())
        self.assertEqual(v.mensaje_caja, "Temporalmente no disponible")


class Reserva(unittest.TestCase):
    """El colchon que evita que la venta siguiente falle a mitad."""

    def test_la_reserva_puede_volver_insuficiente_un_saldo_que_alcanzaba(self) -> None:
        sin_reserva = verificar(_saldo(10_500), CIEN_MXN, ambiente_productivo=True)
        self.assertIs(sin_reserva.suficiencia, Suficiencia.SUFICIENTE)

        con_reserva = verificar(
            _saldo(10_500), CIEN_MXN, ambiente_productivo=True, reserva_cents=100_000
        )
        self.assertIs(con_reserva.suficiencia, Suficiencia.INSUFICIENTE)
        self.assertIn("reserva", con_reserva.motivo)

    def test_por_omision_no_hay_reserva(self) -> None:
        """El valor correcto es una decision comercial; no se inventa aqui."""
        v = verificar(_saldo(10_000), CIEN_MXN, ambiente_productivo=True)
        self.assertIs(v.suficiencia, Suficiencia.SUFICIENTE)


class SaldoDesconocido(unittest.TestCase):
    """No decir el saldo no es lo mismo que no tenerlo."""

    def test_no_es_lo_mismo_que_insuficiente(self) -> None:
        v = verificar(SALDO_NO_REPORTADO, CIEN_MXN, ambiente_productivo=True)
        self.assertIs(v.suficiencia, Suficiencia.DESCONOCIDO)
        self.assertFalse(v.es_bloqueo_duro)

    def test_bloquea_en_produccion(self) -> None:
        """No se puede afirmar que podremos entregar."""
        v = verificar(SALDO_NO_REPORTADO, CIEN_MXN, ambiente_productivo=True)
        self.assertTrue(bloquea(v, ambiente_productivo=True))
        self.assertIn("produccion", v.motivo)

    def test_no_bloquea_en_sandbox(self) -> None:
        """En sandbox el dinero no es real: no hay perdida que prevenir.

        Bloquear aqui pararia el laboratorio por un dato que el proveedor no
        da, sin proteger nada.
        """
        v = verificar(SALDO_NO_REPORTADO, CIEN_MXN, ambiente_productivo=False)
        self.assertFalse(bloquea(v, ambiente_productivo=False))
        self.assertIn("sandbox", v.motivo)

    def test_cero_si_es_una_afirmacion(self) -> None:
        """Cero dice "no tengo"; None dice "no te lo digo". Son distintos."""
        cero = verificar(_saldo(0), CIEN_MXN, ambiente_productivo=False)
        self.assertIs(cero.suficiencia, Suficiencia.INSUFICIENTE)
        self.assertTrue(bloquea(cero, ambiente_productivo=False))


class MonedaDistinta(unittest.TestCase):
    """El monedero de Reloadly esta en USD; las recargas se venden en MXN."""

    def test_no_se_comparan_monedas_distintas(self) -> None:
        """100 USD no son 100 MXN, y aqui no hay tipo de cambio que aplicar."""
        v = verificar(_saldo(10_000, "USD"), CIEN_MXN, ambiente_productivo=True)
        self.assertIs(v.suficiencia, Suficiencia.MONEDA_DISTINTA)
        self.assertIn("USD", v.motivo)
        self.assertIn("MXN", v.motivo)

    def test_un_saldo_grande_en_otra_moneda_tampoco_autoriza(self) -> None:
        """Ni siquiera cuando "obviamente" alcanzaria."""
        v = verificar(_saldo(100_000_000, "USD"), CIEN_MXN, ambiente_productivo=True)
        self.assertIs(v.suficiencia, Suficiencia.MONEDA_DISTINTA)
        self.assertFalse(v.permite_vender)

    def test_bloquea_en_produccion_y_no_en_sandbox(self) -> None:
        v = verificar(_saldo(10_000, "USD"), CIEN_MXN, ambiente_productivo=True)
        self.assertTrue(bloquea(v, ambiente_productivo=True))
        self.assertFalse(bloquea(v, ambiente_productivo=False))


class EntradasInvalidas(unittest.TestCase):
    def test_monto_cero_o_negativo_se_rechaza(self) -> None:
        for cents in (0, -100):
            with self.subTest(cents=cents):
                with self.assertRaises(ValueError):
                    verificar(
                        _saldo(50_000),
                        Money(cents, "MXN"),
                        ambiente_productivo=True,
                    )

    def test_reserva_negativa_se_rechaza(self) -> None:
        with self.assertRaises(ValueError):
            verificar(
                _saldo(50_000),
                CIEN_MXN,
                ambiente_productivo=True,
                reserva_cents=-1,
            )
