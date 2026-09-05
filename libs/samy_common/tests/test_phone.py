"""Pruebas de validacion de numeros telefonicos mexicanos."""

from __future__ import annotations

import pytest

from samy_common.phone import (
    TWO_DIGIT_AREA_CODES,
    PhoneValidationError,
    normalize_mx_phone,
)


class TestFormatosQueLaGenteEscribe:
    @pytest.mark.parametrize(
        "entrada",
        [
            "5512345678",
            "55 1234 5678",
            "55-1234-5678",
            "(55) 1234-5678",
            "+52 55 1234 5678",
            "+525512345678",
            "0052 55 1234 5678",
            "044 55 1234 5678",   # formato viejo de celular
            "045 55 1234 5678",
            "01 55 1234 5678",    # formato viejo de larga distancia
            "+521 55 1234 5678",  # con el 1 despues del codigo de pais
        ],
    )
    def test_todos_dan_el_mismo_e164(self, entrada):
        assert normalize_mx_phone(entrada).e164 == "+525512345678"


class TestLadas:
    @pytest.mark.parametrize("lada", sorted(TWO_DIGIT_AREA_CODES))
    def test_ladas_de_dos_digitos(self, lada):
        telefono = normalize_mx_phone(f"{lada}12345678")
        assert telefono.area_code == lada
        assert len(telefono.subscriber) == 8

    @pytest.mark.parametrize("lada", ["312", "614", "998", "664"])
    def test_ladas_de_tres_digitos(self, lada):
        telefono = normalize_mx_phone(f"{lada}1234567")
        assert telefono.area_code == lada
        assert len(telefono.subscriber) == 7

    def test_colima_es_de_tres_digitos(self):
        assert normalize_mx_phone("3121234567").area_code == "312"


class TestRechazos:
    @pytest.mark.parametrize(
        "entrada,motivo",
        [
            ("", "vacio"),
            ("   ", "solo espacios"),
            ("551234567", "9 digitos"),
            ("55123456789", "11 digitos"),
            ("0512345678", "empieza con 0"),
            ("1111111111", "todos iguales"),
            ("abcdefghij", "sin digitos"),
        ],
    )
    def test_entradas_invalidas(self, entrada, motivo):
        with pytest.raises(PhoneValidationError):
            normalize_mx_phone(entrada)

    def test_el_mensaje_es_util_para_el_cajero(self):
        with pytest.raises(PhoneValidationError) as exc:
            normalize_mx_phone("551234567")
        assert "10 digitos" in str(exc.value)


class TestPresentacion:
    def test_formato_legible_lada_dos(self):
        assert normalize_mx_phone("5512345678").pretty == "55 1234 5678"

    def test_formato_legible_lada_tres(self):
        assert normalize_mx_phone("3121234567").pretty == "312 123 4567"
