"""6% de bono al fondear NO es 6% de descuento por recarga.

TAECEL confirmo su mecanismo: fondear $5,000 MXN deja $5,300 MXN de saldo en
la Bolsa de Tiempo Aire. El descuento se entrega al COMPRAR saldo, no al
gastarlo.

Lo que se gasta en cada recarga es saldo, y cada peso de saldo costo 1/1.06
pesos de efectivo. Asi que una recarga de $100 cuesta

    100 / 1.06 = $94.34      y NO      100 * 0.94 = $94.00

Los 34 centavos son pequenos y el error es SISTEMATICO: modelarlo como
descuento por transaccion hace creer, en todas y cada una de las ventas, que
se gana mas de lo que se gana. Con 6% de bono el descuento efectivo es 5.66%,
no 6%.

La prueba que fija todo esto es ``test_el_bono_no_es_un_descuento``.
"""

from __future__ import annotations

import unittest

from samy_common.pricing import (
    CONEKTA_TARJETA,
    TAECEL_BONO_6,
    ComisionProveedor,
    MecanismoComision,
    MetodoPago,
    PoliticaPrecio,
    cotizar,
    cuota_minima_uniforme,
    efectivo_para_saldo,
    saldo_por_fondeo,
)

CIEN = 10_000
DOSCIENTOS = 20_000
QUINIENTOS = 50_000


class ElEjemploDeTaecel(unittest.TestCase):
    """Lo que TAECEL dijo, comprobado tal cual."""

    def test_cinco_mil_de_fondeo_dan_cinco_mil_trescientos_de_saldo(self) -> None:
        self.assertEqual(saldo_por_fondeo(500_000, TAECEL_BONO_6), 530_000)

    def test_la_inversa_cuadra(self) -> None:
        self.assertEqual(efectivo_para_saldo(530_000, TAECEL_BONO_6), 500_000)

    def test_el_piloto_de_quinientos(self) -> None:
        """$500 de fondeo -> $530 de saldo. Unas cinco recargas de $100."""
        saldo = saldo_por_fondeo(50_000, TAECEL_BONO_6)
        self.assertEqual(saldo, 53_000)
        costo_de_una = TAECEL_BONO_6.costo(CIEN)
        assert costo_de_una is not None
        self.assertEqual(saldo // costo_de_una, 5)


class ElBonoNoEsUnDescuento(unittest.TestCase):
    """La prueba central del archivo."""

    def test_el_bono_no_es_un_descuento(self) -> None:
        """Mismo 6%, dos mecanismos, dos costos distintos."""
        como_descuento = ComisionProveedor(
            bp=600,
            fuente="modelado MAL, a proposito",
            mecanismo=MecanismoComision.DESCUENTO_POR_TRANSACCION,
        )
        self.assertEqual(como_descuento.costo(CIEN), 9_400)  # $94.00
        self.assertEqual(TAECEL_BONO_6.costo(CIEN), 9_434)  # $94.34

        # Los 34 centavos. Y el modelo equivocado siempre da el costo MENOR,
        # o sea que siempre hace creer que se gana mas.
        self.assertEqual(TAECEL_BONO_6.costo(CIEN) - como_descuento.costo(CIEN), 34)

    def test_el_descuento_efectivo_del_seis_por_ciento_es_cinco_sesenta_y_seis(
        self,
    ) -> None:
        self.assertEqual(TAECEL_BONO_6.descuento_efectivo_bp(CIEN), 566)

    def test_el_descuento_efectivo_es_menor_que_el_bono_siempre(self) -> None:
        for bono in (100, 300, 600, 1_000, 2_000, 5_000):
            comision = ComisionProveedor(
                bp=bono, fuente="barrido", mecanismo=MecanismoComision.BONO_AL_FONDEAR
            )
            efectivo = comision.descuento_efectivo_bp(CIEN)
            assert efectivo is not None
            with self.subTest(bono_bp=bono):
                self.assertLess(efectivo, bono)

    def test_el_mecanismo_por_omision_sigue_siendo_el_descuento(self) -> None:
        """No se cambia la conducta de quien no declare mecanismo."""
        vieja = ComisionProveedor(bp=500, fuente="sin declarar mecanismo")
        self.assertIs(vieja.mecanismo, MecanismoComision.DESCUENTO_POR_TRANSACCION)
        self.assertEqual(vieja.costo(CIEN), 9_500)


class MargenRealConElBonoDeTaecel(unittest.TestCase):
    """Que pasa de verdad al vender, con los numeros confirmados."""

    def _margen(self, facial: int, metodo: MetodoPago) -> int:
        cot = cotizar(
            precio_lista_cents=facial,
            metodo=metodo,
            politica=PoliticaPrecio.ABSORBER,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=TAECEL_BONO_6,
        )
        assert cot.margen_cents is not None
        return cot.margen_cents

    def test_en_efectivo_siempre_se_gana(self) -> None:
        """Sin pasarela, el margen es el descuento efectivo: 5.66%."""
        self.assertEqual(self._margen(CIEN, MetodoPago.EFECTIVO), 566)
        self.assertEqual(self._margen(DOSCIENTOS, MetodoPago.EFECTIVO), 1_132)

    def test_cien_con_tarjeta_PIERDE_dinero(self) -> None:
        """El hallazgo que cambia la politica de precios.

        $100 con tarjeta y sin cuota de servicio deja margen NEGATIVO: la
        comision de Conekta ($7.43) se come el descuento de TAECEL ($5.66).
        """
        margen = self._margen(CIEN, MetodoPago.TARJETA)
        self.assertLess(margen, 0)
        self.assertEqual(margen, -177)  # -$1.77 por venta

    def test_doscientos_con_tarjeta_esta_al_filo(self) -> None:
        margen = self._margen(DOSCIENTOS, MetodoPago.TARJETA)
        self.assertEqual(margen, -5)  # -$0.05: practicamente equilibrio

    def test_quinientos_con_tarjeta_si_gana(self) -> None:
        """Las denominaciones grandes perdonan la cuota fija de la pasarela."""
        self.assertGreater(self._margen(QUINIENTOS, MetodoPago.TARJETA), 0)

    def test_la_cuota_de_equilibrio_a_cien_es_de_dos_pesos(self) -> None:
        minima = cuota_minima_uniforme(
            precio_lista_cents=CIEN,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=TAECEL_BONO_6,
        )
        assert minima is not None
        self.assertEqual(minima, 185)  # $1.85

        # Y con esa cuota la venta con tarjeta deja de perder.
        cot = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.CUOTA_UNIFORME,
            cuota_servicio_cents=minima,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=TAECEL_BONO_6,
        )
        assert cot.margen_cents is not None
        self.assertGreaterEqual(cot.margen_cents, 0)
        self.assertTrue(cot.vendible)

    def test_modelarlo_mal_haria_creer_que_cien_con_tarjeta_gana(self) -> None:
        """El costo de equivocarse, en una sola prueba.

        Con el mecanismo equivocado el margen sale positivo y alguien fijaria
        precios creyendo que la tarjeta a $100 es rentable.
        """
        mal = ComisionProveedor(
            bp=600,
            fuente="modelado MAL",
            mecanismo=MecanismoComision.DESCUENTO_POR_TRANSACCION,
        )
        cot_mal = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.ABSORBER,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=mal,
        )
        assert cot_mal.margen_cents is not None
        # El modelo equivocado tambien pierde aqui, pero pierde MENOS...
        self.assertGreater(cot_mal.margen_cents, self._margen(CIEN, MetodoPago.TARJETA))
        # ...y a $200 se cruza la linea: el equivocado dice que gana.
        cot_mal_200 = cotizar(
            precio_lista_cents=DOSCIENTOS,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.ABSORBER,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=mal,
        )
        assert cot_mal_200.margen_cents is not None
        self.assertGreater(cot_mal_200.margen_cents, 0)
        self.assertLess(self._margen(DOSCIENTOS, MetodoPago.TARJETA), 0)


class EntradasInvalidas(unittest.TestCase):
    def test_un_bono_de_mas_del_cien_por_ciento_es_admisible(self) -> None:
        """Fondear $100 y recibir $200 es concebible; un descuento del 100% no."""
        generoso = ComisionProveedor(
            bp=15_000, fuente="hipotetico", mecanismo=MecanismoComision.BONO_AL_FONDEAR
        )
        self.assertEqual(saldo_por_fondeo(10_000, generoso), 25_000)

    def test_comision_desconocida_no_calcula_fondeo(self) -> None:
        desconocida = ComisionProveedor(bp=None, fuente="no se sabe")
        self.assertIsNone(saldo_por_fondeo(500_000, desconocida))
        self.assertIsNone(efectivo_para_saldo(500_000, desconocida))
        self.assertIsNone(desconocida.descuento_efectivo_bp())
