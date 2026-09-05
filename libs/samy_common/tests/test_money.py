"""Pruebas de la aritmetica del dinero.

Estas son las pruebas mas importantes del proyecto. Un error aqui no produce
una pantalla fea: produce descuadres contables.

Cobertura objetivo de este modulo: 100%.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from samy_common.money import (
    CurrencyMismatchError,
    Money,
    from_cents,
    to_cents,
)


class TestConstruccion:
    def test_parse_desde_cadena(self):
        assert Money.parse("300.00").cents == 30000

    def test_parse_desde_decimal(self):
        assert Money.parse(Decimal("10.50")).cents == 1050

    def test_rechaza_float(self):
        """Aceptar float silenciosamente reintroduciria el error de precision
        que esta clase existe para evitar."""
        with pytest.raises(TypeError, match="No se aceptan floats"):
            Money.parse(10.5)

    def test_rechaza_cents_no_entero(self):
        with pytest.raises(TypeError):
            Money(10.5)  # type: ignore[arg-type]

    def test_rechaza_bool_como_cents(self):
        """bool es subclase de int en Python; hay que excluirlo explicitamente."""
        with pytest.raises(TypeError):
            Money(True)  # type: ignore[arg-type]

    def test_moneda_no_soportada(self):
        with pytest.raises(ValueError, match="Moneda no soportada"):
            Money(100, "EUR")


class TestRedondeo:
    @pytest.mark.parametrize(
        "entrada,esperado",
        [
            ("10.004", 1000),
            ("10.005", 1001),  # ROUND_HALF_UP, no HALF_EVEN
            ("10.006", 1001),
            ("0.005", 1),
            ("0.004", 0),
        ],
    )
    def test_redondeo_comercial(self, entrada, esperado):
        """Se usa ROUND_HALF_UP, que es el redondeo comercial esperado en
        Mexico. El default de Decimal (HALF_EVEN) daria 10.005 -> 10.00."""
        assert to_cents(Decimal(entrada)) == esperado

    def test_ida_y_vuelta(self):
        assert from_cents(to_cents(Decimal("1234.56"))) == Decimal("1234.56")


class TestAritmetica:
    def test_suma(self):
        assert (Money.parse("300.00") + Money.parse("10.00")).cents == 31000

    def test_resta(self):
        assert (Money.parse("310.00") - Money.parse("10.00")).cents == 30000

    def test_negativo_es_valido(self):
        """Un monto negativo representa un reembolso o ajuste contable."""
        assert (-Money.parse("10.00")).cents == -1000

    def test_multiplicacion_por_cantidad(self):
        assert (Money.parse("25.00") * 4).cents == 10000

    def test_no_multiplica_por_float(self):
        with pytest.raises(TypeError):
            Money(1000) * 1.5  # type: ignore[operator]

    def test_monedas_distintas_fallan(self):
        with pytest.raises(CurrencyMismatchError):
            Money(100, "MXN") + Money(100, "USD")

    def test_comparaciones(self):
        assert Money(100) < Money(200)
        assert Money(200) > Money(100)
        assert Money(100) <= Money(100)
        assert not bool(Money.zero())
        assert bool(Money(1))


class TestPorcentajes:
    def test_porcentaje_simple(self):
        assert Money.parse("300.00").split_percentage(Decimal("1.5")).cents == 450

    def test_porcentaje_redondea_hacia_arriba_en_medio(self):
        # 1% de 12.345 = 0.12345 -> 12 centavos
        assert Money(1235).split_percentage(Decimal("1")).cents == 12

    def test_rechaza_porcentaje_float(self):
        with pytest.raises(TypeError):
            Money(1000).split_percentage(1.5)  # type: ignore[arg-type]


class TestAllocate:
    """El reparto de comisiones. Aqui es donde se pierden centavos si el
    algoritmo es ingenuo."""

    def test_reparto_exacto_en_tercios(self):
        partes = Money(1000).allocate([1, 1, 1])
        assert sorted(p.cents for p in partes) == [333, 333, 334]
        assert sum(p.cents for p in partes) == 1000

    def test_reparto_ponderado(self):
        tienda, plataforma, proveedor = Money(1000).allocate([60, 30, 10])
        assert (tienda.cents, plataforma.cents, proveedor.cents) == (600, 300, 100)

    @pytest.mark.parametrize("total", [0, 1, 7, 99, 100, 333, 1000, 9999, 100_003])
    @pytest.mark.parametrize(
        "pesos",
        [[1, 1, 1], [60, 30, 10], [7, 3], [1, 0, 0], [33, 33, 34], [999, 1], [1]],
    )
    def test_la_suma_siempre_es_el_total(self, total, pesos):
        """Invariante no negociable: repartir dinero no puede crear ni
        destruir centavos. Se prueba exhaustivamente porque un fallo aqui
        produce descuadres imposibles de rastrear."""
        partes = Money(total).allocate(pesos)
        assert sum(p.cents for p in partes) == total
        assert len(partes) == len(pesos)

    def test_reparto_de_monto_negativo(self):
        partes = Money(-1000).allocate([1, 1, 1])
        assert sum(p.cents for p in partes) == -1000

    def test_pesos_vacios_falla(self):
        with pytest.raises(ValueError):
            Money(1000).allocate([])

    def test_suma_de_pesos_cero_falla(self):
        with pytest.raises(ValueError):
            Money(1000).allocate([0, 0])

    def test_peso_negativo_falla(self):
        with pytest.raises(ValueError):
            Money(1000).allocate([1, -1])


class TestPresentacion:
    def test_str_formatea_con_separadores(self):
        assert str(Money.parse("1234.50")) == "$1,234.50 MXN"

    def test_amount_devuelve_decimal(self):
        assert Money(30000).amount == Decimal("300.00")
