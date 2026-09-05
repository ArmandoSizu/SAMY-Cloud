"""Manejo de dinero para SAMY Cloud.

Regla no negociable: el dinero NUNCA se representa con ``float``.

Internamente todo se almacena y se opera en **centavos enteros** (``int``).
``Decimal`` se usa solo en las fronteras (entrada del usuario, catalogos de
proveedores, presentacion) y siempre se convierte a centavos de inmediato.

Motivo: ``0.1 + 0.2 != 0.3`` en punto flotante binario. En un sistema que mueve
dinero real eso produce descuadres de centavos que son imposibles de conciliar
y que un auditor detecta.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Iterable

__all__ = ["Money", "CurrencyMismatchError", "MXN", "to_cents", "from_cents"]

#: Moneda por defecto de la plataforma.
MXN: Final[str] = "MXN"

#: Monedas soportadas y su numero de decimales (exponente ISO 4217).
_CURRENCY_EXPONENT: Final[dict[str, int]] = {
    "MXN": 2,
    "USD": 2,
}


class CurrencyMismatchError(ValueError):
    """Se intento operar aritmeticamente con dos monedas distintas."""


def _exponent(currency: str) -> int:
    try:
        return _CURRENCY_EXPONENT[currency]
    except KeyError as exc:  # pragma: no cover - defensivo
        raise ValueError(f"Moneda no soportada: {currency!r}") from exc


def to_cents(amount: Decimal | int | str, currency: str = MXN) -> int:
    """Convierte un monto decimal a centavos enteros.

    Usa ROUND_HALF_UP, que es el redondeo comercial esperado en Mexico
    (0.005 -> 0.01), no el ROUND_HALF_EVEN por defecto de ``Decimal``.

    Se rechaza ``float`` explicitamente: aceptarlo silenciosamente seria
    reintroducir el error de precision que esta clase existe para evitar.
    """
    if isinstance(amount, float):  # pragma: no cover - guardia explicita
        raise TypeError(
            "No se aceptan floats para dinero. Usa Decimal('10.50') o centavos int."
        )
    try:
        dec = Decimal(amount) if not isinstance(amount, Decimal) else amount
    except InvalidOperation as exc:
        raise ValueError(f"Monto invalido: {amount!r}") from exc

    if not dec.is_finite():
        raise ValueError(f"Monto no finito: {amount!r}")

    factor = Decimal(10) ** _exponent(currency)
    return int((dec * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int, currency: str = MXN) -> Decimal:
    """Convierte centavos enteros de vuelta a ``Decimal`` para presentacion."""
    factor = Decimal(10) ** _exponent(currency)
    return (Decimal(cents) / factor).quantize(
        Decimal(1).scaleb(-_exponent(currency)), rounding=ROUND_HALF_UP
    )


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """Value object inmutable de dinero.

    ``cents`` es siempre un entero. Un ``Money`` negativo es valido y
    representa un cargo inverso, reembolso o ajuste contable.

    >>> Money.parse("300.00") + Money.parse("10.00")
    Money(cents=31000, currency='MXN')
    >>> Money.parse("100.00").split_percentage(Decimal("2.5"))
    Money(cents=250, currency='MXN')
    """

    cents: int
    currency: str = MXN

    def __post_init__(self) -> None:
        if not isinstance(self.cents, int) or isinstance(self.cents, bool):
            raise TypeError("Money.cents debe ser int (centavos).")
        _exponent(self.currency)

    # -- constructores -------------------------------------------------

    @classmethod
    def parse(cls, amount: Decimal | int | str, currency: str = MXN) -> "Money":
        """Crea un Money desde un monto en unidades mayores ('300.50')."""
        return cls(to_cents(amount, currency), currency)

    @classmethod
    def zero(cls, currency: str = MXN) -> "Money":
        return cls(0, currency)

    # -- conversion ----------------------------------------------------

    @property
    def amount(self) -> Decimal:
        """Monto en unidades mayores, para presentacion y APIs externas."""
        return from_cents(self.cents, self.currency)

    def __str__(self) -> str:
        return f"${self.amount:,.2f} {self.currency}"

    # -- aritmetica ----------------------------------------------------

    def _check(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"No se puede operar {self.currency} con {other.currency}."
            )

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.cents + other.cents, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.cents - other.cents, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.cents, self.currency)

    def __mul__(self, factor: int) -> "Money":
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError("Solo se permite multiplicar dinero por int (cantidad).")
        return Money(self.cents * factor, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._check(other)
        return self.cents < other.cents

    def __le__(self, other: "Money") -> bool:
        self._check(other)
        return self.cents <= other.cents

    def __gt__(self, other: "Money") -> bool:
        self._check(other)
        return self.cents > other.cents

    def __ge__(self, other: "Money") -> bool:
        self._check(other)
        return self.cents >= other.cents

    def __bool__(self) -> bool:
        return self.cents != 0

    @property
    def is_positive(self) -> bool:
        return self.cents > 0

    @property
    def is_zero(self) -> bool:
        return self.cents == 0

    # -- porcentajes ---------------------------------------------------

    def split_percentage(self, percent: Decimal) -> "Money":
        """Calcula un porcentaje de este monto, redondeado a centavo.

        ``percent`` se expresa en puntos porcentuales: ``Decimal("2.5")`` = 2.5%.
        """
        if isinstance(percent, float):  # pragma: no cover
            raise TypeError("El porcentaje debe ser Decimal, no float.")
        raw = (Decimal(self.cents) * Decimal(percent)) / Decimal(100)
        return Money(
            int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), self.currency
        )

    def allocate(self, weights: Iterable[int]) -> list["Money"]:
        """Reparte este monto segun pesos enteros **sin perder centavos**.

        Algoritmo de "largest remainder": reparte la division entera y luego
        asigna los centavos sobrantes uno por uno a las partes con mayor
        residuo. La suma de las partes siempre es exactamente igual al total.

        Es el metodo correcto para dividir una comision entre tienda,
        plataforma y proveedor sin que aparezca ni desaparezca un centavo.

        >>> sum(m.cents for m in Money(1000).allocate([1, 1, 1]))
        1000
        """
        weight_list = list(weights)
        if not weight_list:
            raise ValueError("Se requiere al menos un peso para repartir.")
        if any(w < 0 for w in weight_list):
            raise ValueError("Los pesos no pueden ser negativos.")
        total_weight = sum(weight_list)
        if total_weight == 0:
            raise ValueError("La suma de los pesos no puede ser cero.")

        base = [self.cents * w // total_weight for w in weight_list]
        remainder = self.cents - sum(base)

        # Ordena indices por residuo descendente para repartir los sobrantes.
        residuals = sorted(
            range(len(weight_list)),
            key=lambda i: (self.cents * weight_list[i]) % total_weight,
            reverse=True,
        )
        step = 1 if remainder >= 0 else -1
        for i in range(abs(remainder)):
            base[residuals[i % len(base)]] += step

        return [Money(c, self.currency) for c in base]
