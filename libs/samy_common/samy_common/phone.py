"""Validacion de numeros telefonicos mexicanos para recargas.

Reglas verificables del plan de numeracion mexicano (IFT):

* Un numero nacional tiene **10 digitos**: lada (2 o 3) + numero local.
* Las ladas de 2 digitos son 55 (Valle de Mexico), 33 (Guadalajara) y
  81 (Monterrey). El resto son de 3 digitos.
* Desde agosto de 2019 se marcan 10 digitos siempre, sin 01/044/045.
* El codigo de pais es +52.

Lo que este modulo **no** hace: decidir a que compania pertenece un numero.
La portabilidad numerica en Mexico existe desde 2008, asi que el prefijo NO
determina el operador. Deducirlo del prefijo produciria recargas enviadas a la
compania equivocada. El operador lo elige el cajero y, cuando el proveedor
ofrece consulta de portabilidad, se valida contra su API antes de cobrar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "PhoneValidationError",
    "MexicanPhone",
    "normalize_mx_phone",
    "TWO_DIGIT_AREA_CODES",
]

#: Unicas ladas de dos digitos del plan de numeracion mexicano.
TWO_DIGIT_AREA_CODES: Final[frozenset[str]] = frozenset({"55", "33", "81"})

_NON_DIGIT: Final[re.Pattern[str]] = re.compile(r"\D")


class PhoneValidationError(ValueError):
    """El numero telefonico no cumple el plan de numeracion mexicano."""


@dataclass(frozen=True, slots=True)
class MexicanPhone:
    """Numero mexicano validado y normalizado."""

    national: str  # 10 digitos, sin codigo de pais
    area_code: str
    subscriber: str

    @property
    def e164(self) -> str:
        """Formato internacional E.164, el que esperan los proveedores."""
        return f"+52{self.national}"

    @property
    def pretty(self) -> str:
        """Formato legible para la UI: ``55 1234 5678``."""
        if len(self.area_code) == 2:
            return f"{self.area_code} {self.subscriber[:4]} {self.subscriber[4:]}"
        return f"{self.area_code} {self.subscriber[:3]} {self.subscriber[3:]}"

    def __str__(self) -> str:
        return self.pretty


def normalize_mx_phone(raw: str) -> MexicanPhone:
    """Normaliza y valida un numero mexicano capturado por el cajero.

    Acepta las formas en que la gente realmente escribe un telefono:
    ``5512345678``, ``55 1234 5678``, ``(55) 1234-5678``, ``+52 55 1234 5678``,
    ``0445512345678`` (formato viejo de celular) y ``015512345678``.

    Levanta ``PhoneValidationError`` con un mensaje que el cajero entienda.
    """
    if not raw or not raw.strip():
        raise PhoneValidationError("Escribe el numero telefonico.")

    digits = _NON_DIGIT.sub("", raw)

    # Prefijos historicos y codigo de pais.
    if digits.startswith("044") or digits.startswith("045"):
        digits = digits[3:]
    elif digits.startswith("01"):
        digits = digits[2:]

    if digits.startswith("0052"):
        digits = digits[4:]
    elif len(digits) > 10 and digits.startswith("52"):
        digits = digits[2:]

    # Formato viejo de celular con 1 despues del codigo de pais: +521 55 ...
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) != 10:
        raise PhoneValidationError(
            f"El numero debe tener 10 digitos. Recibimos {len(digits)}."
        )

    if digits[0] == "0":
        raise PhoneValidationError("Un numero mexicano no empieza con 0.")

    area_len = 2 if digits[:2] in TWO_DIGIT_AREA_CODES else 3
    area_code = digits[:area_len]
    subscriber = digits[area_len:]

    if len(set(digits)) == 1:
        raise PhoneValidationError("El numero no puede ser un solo digito repetido.")

    return MexicanPhone(
        national=digits, area_code=area_code, subscriber=subscriber
    )
