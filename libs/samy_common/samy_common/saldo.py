"""Saldo del proveedor: se comprueba ANTES de cobrar, nunca despues.

EL FALLO QUE EVITA
------------------

Los proveedores de recargas son de saldo prepagado. TAECEL lo es, y exige un
fondeo inicial desde $5,000 MXN. Cuando ese saldo se agota, sus recargas
empiezan a fallar.

La secuencia que arruina la jornada es esta:

    1. el cajero cobra $100 al cliente;
    2. la orden pasa a PAID;
    3. se intenta la recarga;
    4. el proveedor responde "saldo insuficiente";
    5. el cliente ya pago y no tiene recarga.

El dinero entro y el servicio no salio. Y no es un caso raro: ocurre
exactamente una vez por cada vez que el saldo se acaba, o sea, tarde o
temprano siempre.

La regla es que el saldo se comprueba en ``create_fulfillment``, que corre
ANTES de que exista la orden. Sin cumplimiento no hay orden, y sin orden no
hay nada que cobrar. Comprobarlo en ``execute_topup`` seria comprobarlo
despues del paso 2, o sea demasiado tarde.

LAS CUATRO RESPUESTAS, Y POR QUE NO SON DOS
-------------------------------------------

"Hay saldo" y "no hay saldo" no alcanzan. Hacen falta dos mas, y las dos
importan:

``DESCONOCIDO``
    El proveedor no dice su saldo. No es lo mismo que no tenerlo, y tampoco
    es permiso para vender: no se puede afirmar que podremos entregar.

``MONEDA_DISTINTA``
    El saldo esta en otra moneda que la venta. Es el caso real de Reloadly:
    su monedero es en USD y la recarga en MXN. Comparar 100 USD con 100 MXN
    da "suficiente" cuando en realidad hay veinte veces mas de lo que se
    cree... o al contrario. Convertir exigiria un tipo de cambio que nadie
    nos dio, y aplicar uno inventado es justo lo que este proyecto prohibe.

QUE SE BLOQUEA Y DONDE
----------------------

``INSUFICIENTE`` bloquea SIEMPRE, en cualquier ambiente. Es la regla dura.

``DESCONOCIDO`` y ``MONEDA_DISTINTA`` bloquean solo en produccion. El motivo
no es comodidad: en sandbox el dinero no es real, asi que un saldo que no se
puede verificar no puede producir una perdida; en produccion si. Bloquear
tambien en sandbox pararia el laboratorio por un dato que Reloadly no da en
la moneda que haria falta, sin proteger nada.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

from samy_common.money import CurrencyMismatchError, Money

__all__ = [
    "Suficiencia",
    "SaldoProveedor",
    "Veredicto",
    "SALDO_NO_REPORTADO",
    "verificar",
    "bloquea",
]


class Suficiencia(enum.StrEnum):
    SUFICIENTE = "SUFICIENTE"
    #: El proveedor dijo su saldo y no alcanza. Bloquea siempre.
    INSUFICIENTE = "INSUFICIENTE"
    #: El proveedor no dijo su saldo. No es lo mismo que no tenerlo.
    DESCONOCIDO = "DESCONOCIDO"
    #: El saldo esta en otra moneda. No se convierte con un tipo inventado.
    MONEDA_DISTINTA = "MONEDA_DISTINTA"


@dataclass(frozen=True, slots=True)
class SaldoProveedor:
    """Lo que el proveedor dijo que tiene, y cuando lo dijo.

    ``disponible=None`` significa **no lo dijo**. Se distingue de cero a
    proposito: cero es una afirmacion ("no tengo saldo") y None es la
    ausencia de afirmacion. Tratarlos igual llevaria a bloquear ventas por un
    proveedor que si tiene fondos, o -mucho peor- a venderlas creyendo que
    los tiene.
    """

    disponible: Money | None
    #: Texto para una persona: de donde salio el dato, o por que falta.
    detalle: str = ""

    @property
    def conocido(self) -> bool:
        return self.disponible is not None


#: Constante con nombre para el caso "el proveedor no reporta saldo". Existe
#: para que aparezca explicito en el codigo en vez de un None suelto que
#: parezca un olvido.
SALDO_NO_REPORTADO: Final[SaldoProveedor] = SaldoProveedor(
    disponible=None, detalle="El proveedor no reporta saldo."
)


@dataclass(frozen=True, slots=True)
class Veredicto:
    suficiencia: Suficiencia
    #: Para el panel de administracion: dice exactamente que pasa.
    motivo: str
    #: Para la caja. No menciona proveedores ni saldos: al cajero no le sirve
    #: saber que TAECEL esta sin fondos, le sirve saber que hoy no lo venda.
    mensaje_caja: str = "Temporalmente no disponible"

    @property
    def permite_vender(self) -> bool:
        return self.suficiencia is Suficiencia.SUFICIENTE

    @property
    def es_bloqueo_duro(self) -> bool:
        """Insuficiente confirmado. No depende del ambiente."""
        return self.suficiencia is Suficiencia.INSUFICIENTE


def verificar(
    saldo: SaldoProveedor,
    requerido: Money,
    *,
    ambiente_productivo: bool,
    reserva_cents: int = 0,
) -> Veredicto:
    """Decide si se puede vender ``requerido`` con el saldo que hay.

    ``reserva_cents`` es un colchon que no se toca. Vender hasta dejar el
    saldo exactamente en cero hace que la venta siguiente falle a mitad de
    camino, con la orden ya pagada: justo el fallo que este modulo evita, un
    turno mas tarde. Por omision es cero porque el valor correcto es una
    decision comercial y no se inventa aqui.
    """
    if requerido.cents <= 0:
        raise ValueError("El monto requerido tiene que ser positivo.")
    if reserva_cents < 0:
        raise ValueError("La reserva no puede ser negativa.")

    if saldo.disponible is None:
        return Veredicto(
            suficiencia=Suficiencia.DESCONOCIDO,
            motivo=(
                "No se pudo verificar el saldo del proveedor. "
                + (saldo.detalle or "No lo reporta.")
                + (
                    " En produccion no se vende sin verificarlo."
                    if ambiente_productivo
                    else " En sandbox se permite: el dinero no es real."
                )
            ),
        )

    if saldo.disponible.currency != requerido.currency:
        # No se convierte. Un tipo de cambio inventado aqui produciria un
        # "hay saldo" que nadie podria auditar.
        return Veredicto(
            suficiencia=Suficiencia.MONEDA_DISTINTA,
            motivo=(
                f"El saldo del proveedor esta en {saldo.disponible.currency} y "
                f"la venta en {requerido.currency}. No se convierte con un tipo "
                "de cambio que el proveedor no dio."
            ),
        )

    # Desde aqui las dos monedas coinciden, asi que la resta es legitima.
    try:
        margen = saldo.disponible - requerido
    except CurrencyMismatchError:  # pragma: no cover - ya se comprobo arriba
        raise

    if margen.cents < reserva_cents:
        if reserva_cents:
            detalle = (
                f"Saldo {saldo.disponible}, venta {requerido}, quedarian "
                f"{margen} y la reserva exige {Money(reserva_cents, requerido.currency)}."
            )
        else:
            detalle = f"Saldo {saldo.disponible}, venta {requerido}."
        return Veredicto(
            suficiencia=Suficiencia.INSUFICIENTE,
            motivo="Saldo insuficiente en el proveedor. " + detalle,
        )

    return Veredicto(
        suficiencia=Suficiencia.SUFICIENTE,
        motivo=f"Saldo {saldo.disponible}, suficiente para {requerido}.",
        mensaje_caja="",
    )


def bloquea(veredicto: Veredicto, *, ambiente_productivo: bool) -> bool:
    """Si este veredicto impide vender en ESTE ambiente.

    Separado de ``verificar`` a proposito: el veredicto es un hecho sobre el
    saldo y no cambia; que ese hecho bloquee o no es una politica. Tenerlos
    juntos haria imposible registrar "no se pudo verificar" en sandbox sin
    perder la informacion.
    """
    if veredicto.permite_vender:
        return False
    if veredicto.es_bloqueo_duro:
        return True
    return ambiente_productivo
