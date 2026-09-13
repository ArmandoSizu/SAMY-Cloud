"""El mismo 6% da tres costos distintos. Y a veces ninguno.

Este archivo existe porque el error mas caro de esta parte del sistema no es
equivocarse en el porcentaje: es creer que saber el porcentaje es saber el
costo.

    TAECEL confirmo 6% de BONO AL FONDEAR       -> $100 cuesta $94.34
    Un descuento por transaccion del 6%          -> $100 cuesta $94.00
    Una comision del 6% abonada a otra bolsa     -> $100 cuesta $100.00
    Un 6% cuyo mecanismo nadie ha comprobado     -> no se sabe

Las cuatro filas son la misma frase comercial -"te damos el 6%"- y producen
cuatro decisiones de precio distintas. La cuarta es la de Linntae hoy.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from samy_common.pricing import (
    CONEKTA_TARJETA,
    ComisionProveedor,
    ConfiguracionDePrecioInvalida,
    MecanismoComision,
    MetodoPago,
    PoliticaPrecio,
    cotizar,
    cuota_minima_uniforme,
    efectivo_para_saldo,
    saldo_por_fondeo,
)

CIEN = 10_000


def _comision(mecanismo: MecanismoComision, bp: int = 600) -> ComisionProveedor:
    return ComisionProveedor(bp=bp, fuente="prueba", mecanismo=mecanismo)


class TestElMismoSeisPorCiento:
    """Cuatro mecanismos, cuatro costos. Es la prueba central del modulo."""

    def test_los_cuatro_costos_de_una_recarga_de_cien(self) -> None:
        esperados = {
            MecanismoComision.DESCUENTO_POR_TRANSACCION: 9_400,
            MecanismoComision.BONO_AL_FONDEAR: 9_434,
            MecanismoComision.COMISION_ACREDITADA_APARTE: 10_000,
            MecanismoComision.SIN_DETERMINAR: None,
        }
        for mecanismo, esperado in esperados.items():
            assert _comision(mecanismo).costo(CIEN) == esperado, mecanismo

    def test_saber_el_porcentaje_no_es_saber_el_costo(self) -> None:
        """SIN_DETERMINAR devuelve None aunque ``bp`` este puesto.

        Si esta prueba se cae devolviendo un numero, el sistema empezaria a
        afirmar un margen que nadie comprobo, y alguien fijaria precios con
        el.
        """
        comision = _comision(MecanismoComision.SIN_DETERMINAR)
        assert comision.conocida is True
        assert comision.bp == 600
        assert comision.costo(CIEN) is None
        assert comision.descuento_efectivo_bp(CIEN) is None


class TestComisionAcreditadaAparte:
    """El saldo se gasta completo y la comision cae en otra bolsa."""

    def test_el_costo_inmediato_es_el_valor_facial(self) -> None:
        comision = _comision(MecanismoComision.COMISION_ACREDITADA_APARTE)
        assert comision.costo(CIEN) == CIEN

    def test_el_descuento_efectivo_inmediato_es_cero(self) -> None:
        """Y eso es correcto: esta recarga no salio mas barata.

        La comision existe, pero esta en una bolsa distinta, y contarla como
        descuento de esta venta seria contar dinero que quiza no se puede
        usar todavia.
        """
        comision = _comision(MecanismoComision.COMISION_ACREDITADA_APARTE)
        assert comision.descuento_efectivo_bp(CIEN) == 0

    def test_la_comision_acreditada_se_reporta_aparte(self) -> None:
        comision = _comision(MecanismoComision.COMISION_ACREDITADA_APARTE)
        assert comision.comision_acreditada_cents(CIEN) == 600

    def test_los_otros_mecanismos_no_reportan_comision_acreditada(self) -> None:
        """Devolverla tambien ahi la contaria dos veces."""
        for mecanismo in (
            MecanismoComision.DESCUENTO_POR_TRANSACCION,
            MecanismoComision.BONO_AL_FONDEAR,
            MecanismoComision.SIN_DETERMINAR,
        ):
            assert _comision(mecanismo).comision_acreditada_cents(CIEN) is None

    def test_una_comision_sobre_venta_de_cien_por_ciento_se_rechaza(self) -> None:
        with pytest.raises(ConfiguracionDePrecioInvalida):
            _comision(MecanismoComision.COMISION_ACREDITADA_APARTE, bp=10_000)


class TestFondeo:
    def test_sin_mecanismo_determinado_no_se_afirma_la_relacion_de_fondeo(self) -> None:
        """Devolver uno a uno seria afirmar que no hay bono.

        Nadie lo ha comprobado, y con esa afirmacion alguien planearia un
        fondeo con menos saldo del que necesita, o con mas.
        """
        comision = _comision(MecanismoComision.SIN_DETERMINAR)
        assert saldo_por_fondeo(500_000, comision) is None
        assert efectivo_para_saldo(500_000, comision) is None

    def test_con_comision_aparte_el_fondeo_es_uno_a_uno(self) -> None:
        comision = _comision(MecanismoComision.COMISION_ACREDITADA_APARTE)
        assert saldo_por_fondeo(500_000, comision) == 500_000
        assert efectivo_para_saldo(500_000, comision) == 500_000

    def test_el_bono_sigue_dando_el_ejemplo_de_taecel(self) -> None:
        """No se rompio nada de lo que ya funcionaba: $5,000 -> $5,300."""
        comision = _comision(MecanismoComision.BONO_AL_FONDEAR)
        assert saldo_por_fondeo(500_000, comision) == 530_000


class TestCotizacion:
    def test_con_mecanismo_sin_determinar_el_margen_es_desconocido(self) -> None:
        cotizacion = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.EFECTIVO,
            politica=PoliticaPrecio.ABSORBER,
            comision_proveedor=_comision(MecanismoComision.SIN_DETERMINAR),
        )
        assert cotizacion.margen_conocido is False
        assert cotizacion.margen_cents is None
        assert any("DESCONOCIDO" in a for a in cotizacion.advertencias)
        # Y se puede vender: bloquear aqui dejaria el negocio parado
        # esperando una medicion, con la advertencia ya escrita.
        assert cotizacion.vendible is True

    def test_con_comision_aparte_el_margen_en_efectivo_es_cero(self) -> None:
        """Es la consecuencia honesta de ese mecanismo, y hay que verla.

        Vender una recarga de $100 en efectivo sin cuota deja cero de margen
        inmediato: la ganancia esta en la bolsa de comision. Quien fije
        precios tiene que saberlo antes, no al cerrar el mes.
        """
        cotizacion = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.EFECTIVO,
            politica=PoliticaPrecio.ABSORBER,
            comision_proveedor=_comision(MecanismoComision.COMISION_ACREDITADA_APARTE),
        )
        assert cotizacion.costo_proveedor_cents == CIEN
        assert cotizacion.margen_cents == 0

    def test_con_comision_aparte_la_tarjeta_pierde_toda_la_comision(self) -> None:
        """$100 con tarjeta: el costo de Conekta sale del bolsillo, entero.

        Con bono al fondear la perdida a $100 era de $1.77 porque el
        descuento cubria parte. Si la comision se abona aparte, la perdida es
        la comision completa de la pasarela.
        """
        cotizacion = cotizar(
            precio_lista_cents=CIEN,
            metodo=MetodoPago.TARJETA,
            politica=PoliticaPrecio.ABSORBER,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=_comision(MecanismoComision.COMISION_ACREDITADA_APARTE),
        )
        assert cotizacion.margen_cents == -cotizacion.costo_pasarela_cents
        assert cotizacion.costo_pasarela_cents == 743

    def test_la_cuota_de_equilibrio_es_desconocida_si_el_mecanismo_lo_es(self) -> None:
        assert (
            cuota_minima_uniforme(
                precio_lista_cents=CIEN,
                tarifa=CONEKTA_TARJETA,
                comision_proveedor=_comision(MecanismoComision.SIN_DETERMINAR),
            )
            is None
        )

    def test_la_cuota_de_equilibrio_con_comision_aparte_cubre_toda_la_pasarela(
        self,
    ) -> None:
        cuota = cuota_minima_uniforme(
            precio_lista_cents=CIEN,
            tarifa=CONEKTA_TARJETA,
            comision_proveedor=_comision(MecanismoComision.COMISION_ACREDITADA_APARTE),
        )
        assert cuota is not None
        # Se verifica contra la funcion de costo real, no contra el algebra.
        total = CIEN + cuota
        assert total - CONEKTA_TARJETA.costo(total) - CIEN >= 0
        # Y un centavo menos ya no alcanza: es el minimo de verdad.
        if cuota > 0:
            total_menor = CIEN + cuota - 1
            assert total_menor - CONEKTA_TARJETA.costo(total_menor) - CIEN < 0


class TestNoSeRompioNadaDeLoAnterior:
    """El mecanismo por omision sigue siendo el descuento por transaccion.

    Es lo que garantiza que agregar dos mecanismos no cambio la conducta de
    ningun calculo existente.
    """

    def test_sin_declarar_mecanismo_es_descuento_por_transaccion(self) -> None:
        comision = ComisionProveedor(bp=600, fuente="prueba")
        assert comision.mecanismo is MecanismoComision.DESCUENTO_POR_TRANSACCION
        assert comision.costo(CIEN) == 9_400

    def test_una_comision_desconocida_sigue_siendo_desconocida(self) -> None:
        comision = ComisionProveedor(bp=None, fuente="no la publican")
        assert comision.conocida is False
        assert comision.costo(CIEN) is None
        assert comision.comision_acreditada_cents(CIEN) is None

    def test_el_descuento_por_transaccion_de_cien_por_ciento_sigue_rechazado(self) -> None:
        with pytest.raises(ConfiguracionDePrecioInvalida):
            ComisionProveedor(bp=10_000, fuente="imposible")

    def test_el_bono_de_mas_de_cien_por_ciento_sigue_siendo_concebible(self) -> None:
        """Fondear $100 y recibir $200 es raro, pero no es una contradiccion."""
        comision = ComisionProveedor(
            bp=10_000, fuente="promocion", mecanismo=MecanismoComision.BONO_AL_FONDEAR
        )
        assert comision.costo(CIEN) == 5_000

    def test_los_puntos_base_siguen_siendo_enteros_y_no_floats(self) -> None:
        comision = ComisionProveedor(
            bp=566,
            fuente="prueba",
            mecanismo=MecanismoComision.DESCUENTO_POR_TRANSACCION,
        )
        costo = comision.costo(CIEN)
        assert isinstance(costo, int)
        assert not isinstance(Decimal(costo), float)
