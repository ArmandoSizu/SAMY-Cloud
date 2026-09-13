"""El motor de precios, y sobre todo lo que tiene prohibido hacer.

La prueba mas importante de este archivo es
``NoEsUnRecargoPorTarjeta.test_la_cuota_es_identica_en_los_dos_metodos``.
Los Terminos de Conekta prohiben cobrar mas por pagar con tarjeta, y la
sancion es suspension de cuenta. Si esa prueba se cae, el sistema esta
violando el contrato de su propia pasarela de pagos.

Estas pruebas corren con unittest de la libreria estandar, sin pytest y sin
Django, porque el modulo que prueban tambien es puro. Eso permite verificar la
aritmetica del dinero sin levantar nada.
"""

from __future__ import annotations

import unittest

from samy_common.pricing import (
    CONEKTA_TARJETA,
    ComisionProveedor,
    ConfiguracionDePrecioInvalida,
    Cotizacion,
    MetodoDePagoNoPermitido,
    MetodoPago,
    PoliticaPrecio,
    TarifaPasarela,
    cotizar,
    cuota_minima_uniforme,
)

CIEN = 10_000  # $100.00 en centavos

#: 5% de comision. Valor de EJEMPLO para las pruebas: TAECEL no publica el suyo.
COMISION_5 = ComisionProveedor(bp=500, fuente="ejemplo de prueba")
DESCONOCIDA = ComisionProveedor(bp=None, fuente="TAECEL no lo publica")


class TarifaDeLaPasarela(unittest.TestCase):
    def test_cien_pesos_con_conekta_cuesta_743_centavos(self) -> None:
        """3.4% + $3 + IVA sobre $100, redondeando los costos hacia arriba.

        La tarifa "de calculadora" da $7.424. Aqui son $7.43 porque los costos
        se redondean hacia arriba a proposito.
        """
        self.assertEqual(CONEKTA_TARJETA.costo(CIEN), 743)

    def test_el_efectivo_no_cuesta_comision(self) -> None:
        cot = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.EFECTIVO,
            politica=PoliticaPrecio.ABSORBER,
            comision_proveedor=COMISION_5,
        )
        self.assertEqual(cot.costo_pasarela_cents, 0)

    def test_la_comision_se_calcula_sobre_el_total_no_sobre_el_precio(self) -> None:
        """La cuota de servicio tambien paga comision.

        Es el error que deja el margen corto en cada venta: calcular la
        comision sobre los $100 cuando por la pasarela pasaron $102.53.
        """
        con_cuota = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.CUOTA_UNIFORME,
            cuota_servicio_cents=500,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=COMISION_5,
        )
        self.assertGreater(con_cuota.costo_pasarela_cents, CONEKTA_TARJETA.costo(CIEN))
        self.assertEqual(
            con_cuota.costo_pasarela_cents, CONEKTA_TARJETA.costo(CIEN + 500)
        )

    def test_no_se_aceptan_floats_en_la_tarifa(self) -> None:
        with self.assertRaises(ConfiguracionDePrecioInvalida):
            TarifaPasarela(porcentaje_bp=3.4, fija_cents=300)  # type: ignore[arg-type]

    def test_una_comision_de_cien_por_ciento_se_rechaza(self) -> None:
        with self.assertRaises(ConfiguracionDePrecioInvalida):
            TarifaPasarela(porcentaje_bp=10_000, fija_cents=0)


class NoEsUnRecargoPorTarjeta(unittest.TestCase):
    """La restriccion contractual, convertida en prueba."""

    def _cotizar(self, metodo: MetodoPago) -> Cotizacion:
        return cotizar(
            precio_lista_cents=CIEN,
            metodo=metodo,
            politica=PoliticaPrecio.CUOTA_UNIFORME,
            cuota_servicio_cents=300,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=COMISION_5,
        )

    def test_la_cuota_es_identica_en_los_dos_metodos(self) -> None:
        """Si esta falla, estamos violando los Terminos de Conekta."""
        efectivo = self._cotizar(MetodoPago.EFECTIVO)
        tarjeta = self._cotizar(MetodoPago.TARJETA)
        self.assertEqual(efectivo.cuota_servicio_cents, tarjeta.cuota_servicio_cents)

    def test_el_cliente_paga_lo_mismo_en_los_dos_metodos(self) -> None:
        efectivo = self._cotizar(MetodoPago.EFECTIVO)
        tarjeta = self._cotizar(MetodoPago.TARJETA)
        self.assertEqual(efectivo.total_cents, tarjeta.total_cents)

    def test_lo_que_cambia_es_el_margen_no_el_precio(self) -> None:
        """El costo de la tarjeta lo absorbe el negocio, no el cliente."""
        efectivo = self._cotizar(MetodoPago.EFECTIVO)
        tarjeta = self._cotizar(MetodoPago.TARJETA)
        self.assertEqual(efectivo.total_cents, tarjeta.total_cents)
        assert efectivo.margen_cents is not None and tarjeta.margen_cents is not None
        self.assertGreater(efectivo.margen_cents, tarjeta.margen_cents)

    def test_no_existe_forma_de_pedir_una_cuota_por_metodo(self) -> None:
        """La firma de cotizar() no admite una cuota distinta por metodo.

        Se comprueba por introspeccion y no leyendo el codigo: una validacion
        se puede quitar, un parametro que no existe no se puede usar. Si
        alguien anade un parametro con "metodo" y "cuota" en el nombre, esta
        prueba lo detiene y obliga a leer los Terminos de Conekta primero.
        """
        import inspect

        parametros = set(inspect.signature(cotizar).parameters)
        sospechosos = [
            p
            for p in parametros
            if ("cuota" in p or "fee" in p or "recargo" in p)
            and ("metodo" in p or "tarjeta" in p or "card" in p)
        ]
        self.assertEqual(sospechosos, [])
        self.assertIn("cuota_servicio_cents", parametros)


class PoliticaSoloEfectivo(unittest.TestCase):
    def test_la_tarjeta_se_rechaza(self) -> None:
        with self.assertRaises(MetodoDePagoNoPermitido):
            cotizar(
                precio_lista_cents=CIEN,
                metodo=MetodoPago.TARJETA,
                politica=PoliticaPrecio.SOLO_EFECTIVO,
                comision_proveedor=COMISION_5,
            )

    def test_el_efectivo_funciona_y_sin_cuota(self) -> None:
        cot = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.EFECTIVO,
            politica=PoliticaPrecio.SOLO_EFECTIVO,
            comision_proveedor=COMISION_5,
        )
        self.assertEqual(cot.total_cents, CIEN)
        self.assertEqual(cot.cuota_servicio_cents, 0)
        self.assertTrue(cot.vendible)
        self.assertEqual(cot.margen_cents, 500)  # 5% de $100, sin costo de pasarela


class MargenDesconocido(unittest.TestCase):
    """Cuando falta un costo no se estima. Es el caso de TAECEL hoy."""

    def _cotizar(self) -> Cotizacion:
        return cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.CUOTA_UNIFORME,
            cuota_servicio_cents=300,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=DESCONOCIDA,
        )

    def test_el_margen_es_none_no_cero(self) -> None:
        """Cero seria afirmar "no ganamos ni perdemos". No hay tal afirmacion."""
        cot = self._cotizar()
        self.assertIsNone(cot.margen_cents)
        self.assertFalse(cot.margen_conocido)

    def test_es_perdida_tambien_es_none(self) -> None:
        self.assertIsNone(self._cotizar().es_perdida)

    def test_lo_advierte_por_escrito(self) -> None:
        cot = self._cotizar()
        self.assertTrue(any("DESCONOCIDO" in a for a in cot.advertencias))
        self.assertTrue(any("TAECEL" in a for a in cot.advertencias))

    def test_se_puede_vender_de_todos_modos(self) -> None:
        """Bloquear aqui dejaria el negocio parado esperando un dato que
        TAECEL no publica. La advertencia queda escrita; la venta procede."""
        self.assertTrue(self._cotizar().vendible)

    def test_no_hay_cuota_de_equilibrio_calculable(self) -> None:
        self.assertIsNone(
            cuota_minima_uniforme(
                precio_lista_cents=CIEN,
                tarifa=CONEKTA_TARJETA,
                comision_proveedor=DESCONOCIDA,
            )
        )


class CuotaDeEquilibrio(unittest.TestCase):
    """El numero que hace accionable la politica CUOTA_UNIFORME."""

    def test_la_cuota_minima_realmente_no_pierde(self) -> None:
        """Se verifica cotizando con ella, no confiando en el algebra."""
        for bp in (0, 100, 300, 500, 742, 800, 1500, 3000):
            comision = ComisionProveedor(bp=bp, fuente="barrido de prueba")
            minima = cuota_minima_uniforme(
                precio_lista_cents=CIEN,
                tarifa=CONEKTA_TARJETA,
                comision_proveedor=comision,
            )
            assert minima is not None
            cot = cotizar(
                precio_lista_cents=CIEN,
                metodo=MetodoPago.TARJETA,
                politica=PoliticaPrecio.CUOTA_UNIFORME,
                cuota_servicio_cents=max(1, minima),
                tarifa=CONEKTA_TARJETA,
                comision_proveedor=comision,
            )
            assert cot.margen_cents is not None
            with self.subTest(comision_bp=bp):
                self.assertGreaterEqual(cot.margen_cents, 0)
                self.assertTrue(cot.vendible)

    def test_un_centavo_menos_si_pierde(self) -> None:
        """Que sea el MINIMO, no solo un valor que funciona."""
        comision = ComisionProveedor(bp=500, fuente="prueba")
        minima = cuota_minima_uniforme(
            precio_lista_cents=CIEN,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=comision,
        )
        assert minima is not None and minima > 0
        cot = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.CUOTA_UNIFORME,
            cuota_servicio_cents=minima - 1,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=comision,
        )
        assert cot.margen_cents is not None
        self.assertLess(cot.margen_cents, 0)

    def test_mas_comision_del_proveedor_significa_menos_cuota(self) -> None:
        cuotas = [
            cuota_minima_uniforme(
                precio_lista_cents=CIEN,
                tarifa=CONEKTA_TARJETA,
                comision_proveedor=ComisionProveedor(bp=bp, fuente="p"),
            )
            for bp in (100, 500, 1000)
        ]
        self.assertEqual(cuotas, sorted(cuotas, reverse=True))

    def test_con_comision_alta_no_hace_falta_cuota(self) -> None:
        """Si el proveedor paga mas que la pasarela, la cuota es cero."""
        minima = cuota_minima_uniforme(
            precio_lista_cents=CIEN,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=ComisionProveedor(bp=1500, fuente="p"),
        )
        self.assertEqual(minima, 0)


class PoliticaAbsorber(unittest.TestCase):
    def test_la_perdida_es_deliberada_y_se_puede_vender(self) -> None:
        cot = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.ABSORBER,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=ComisionProveedor(bp=100, fuente="p"),
        )
        self.assertTrue(cot.es_perdida)
        self.assertTrue(cot.vendible)
        self.assertTrue(any("DELIBERADA" in a for a in cot.advertencias))

    def test_no_se_le_cobra_cuota_al_cliente(self) -> None:
        cot = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.ABSORBER,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=COMISION_5,
        )
        self.assertEqual(cot.total_cents, CIEN)


class PerdidaBajoCuotaUniforme(unittest.TestCase):
    """Ahi la perdida no es una decision, es una cuota mal calculada."""

    def _cotizar(self) -> Cotizacion:
        return cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.CUOTA_UNIFORME,
            cuota_servicio_cents=1,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=ComisionProveedor(bp=100, fuente="p"),
        )

    def test_no_es_vendible(self) -> None:
        cot = self._cotizar()
        self.assertTrue(cot.es_perdida)
        self.assertFalse(cot.vendible)

    def test_la_advertencia_trae_el_numero_que_lo_arregla(self) -> None:
        cot = self._cotizar()
        minima = cuota_minima_uniforme(
            precio_lista_cents=CIEN,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=ComisionProveedor(bp=100, fuente="p"),
        )
        self.assertTrue(any("PERDIDA" in a for a in cot.advertencias))
        self.assertTrue(any(str(minima) in a for a in cot.advertencias))


class FailClosed(unittest.TestCase):
    """Sin configuracion no hay cotizacion. El motor no elige por nadie."""

    def test_cuota_uniforme_en_cero_se_rechaza(self) -> None:
        with self.assertRaises(ConfiguracionDePrecioInvalida):
            cotizar(
                precio_lista_cents=CIEN,
                metodo=MetodoPago.EFECTIVO,
                politica=PoliticaPrecio.CUOTA_UNIFORME,
                cuota_servicio_cents=0,
                comision_proveedor=COMISION_5,
            )

    def test_absorber_con_cuota_se_rechaza(self) -> None:
        """Absorber significa que paga el negocio, no el cliente."""
        with self.assertRaises(ConfiguracionDePrecioInvalida):
            cotizar(
                precio_lista_cents=CIEN,
                metodo=MetodoPago.EFECTIVO,
                politica=PoliticaPrecio.ABSORBER,
                cuota_servicio_cents=300,
                comision_proveedor=COMISION_5,
            )

    def test_tarjeta_sin_tarifa_se_rechaza(self) -> None:
        """Una tarifa supuesta produce un margen supuesto."""
        with self.assertRaises(ConfiguracionDePrecioInvalida):
            cotizar(
                precio_lista_cents=CIEN,
                metodo=MetodoPago.TARJETA,
                politica=PoliticaPrecio.ABSORBER,
                tarifa=None,
                comision_proveedor=COMISION_5,
            )

    def test_no_se_aceptan_floats_de_precio(self) -> None:
        with self.assertRaises(ConfiguracionDePrecioInvalida):
            cotizar(
                precio_lista_cents=100.0,  # type: ignore[arg-type]
                metodo=MetodoPago.EFECTIVO,
                politica=PoliticaPrecio.ABSORBER,
                comision_proveedor=COMISION_5,
            )

    def test_precio_cero_o_negativo_se_rechaza(self) -> None:
        for precio in (0, -100):
            with self.subTest(precio=precio):
                with self.assertRaises(ConfiguracionDePrecioInvalida):
                    cotizar(
                        precio_lista_cents=precio,
                        metodo=MetodoPago.EFECTIVO,
                        politica=PoliticaPrecio.ABSORBER,
                        comision_proveedor=COMISION_5,
                    )

    def test_comision_de_proveedor_invalida_se_rechaza(self) -> None:
        for bp in (-1, 10_000, 20_000):
            with self.subTest(bp=bp):
                with self.assertRaises(ConfiguracionDePrecioInvalida):
                    ComisionProveedor(bp=bp)

    def test_la_cotizacion_es_inmutable(self) -> None:
        """Un total que se puede reasignar despues de cotizar es un total que
        alguien va a reasignar."""
        cot = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.EFECTIVO,
            politica=PoliticaPrecio.ABSORBER,
            comision_proveedor=COMISION_5,
        )
        with self.assertRaises(Exception):
            cot.total_cents = 1  # type: ignore[misc]
