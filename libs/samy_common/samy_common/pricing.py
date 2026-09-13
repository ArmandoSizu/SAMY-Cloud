"""Motor de precios de SAMY Cloud.

Modulo PURO a proposito: no importa Django, no toca la base de datos y no lee
configuracion. Recibe numeros y devuelve numeros. Todo en centavos enteros.

Esa pureza no es estetica. El calculo de margen es lo que decide si el negocio
gana o pierde en cada venta, y tiene que poder probarse y auditarse sin
levantar un contenedor. La capa que lee la politica de ``settings`` es otra
cosa y vive en otro archivo.

EL PROBLEMA QUE RESUELVE
------------------------

Los Terminos y Condiciones de Conekta (Suplemento de Tarjetas, seccion de
Prohibiciones) **prohiben el recargo por pagar con tarjeta**: esta vedado
"aplicar cargos adicionales al precio de los productos y/o servicios por
aceptar pagos mediante la plataforma Conekta" y "cobrar a los Clientes
comisiones por pago con tarjetas de debito o credito". Violarlo causa
suspension de cuenta.

Y sin embargo cobrar $100 con tarjeta le cuesta al negocio alrededor de $7.43
de comision. Si la comision que TAECEL nos devuelve es menor a eso, **cada
recarga con tarjeta pierde dinero**.

De ahi que existan exactamente tres politicas legitimas, y ninguna es un
recargo por tarjeta:

``SOLO_EFECTIVO``
    Las recargas se cobran unicamente en efectivo. La tarjeta se rechaza para
    este producto, no se le cobra mas.

``CUOTA_UNIFORME``
    Una cuota de servicio **identica en todos los metodos de pago**. No es un
    recargo por tarjeta porque quien paga en efectivo paga exactamente lo
    mismo. Es el precio del servicio.

``ABSORBER``
    El negocio se come el costo. Legitimo y a veces correcto (adquirir
    clientes), pero tiene que ser una decision consciente y verse en el
    margen, no una sorpresa a fin de mes.

LO QUE ESTE MODULO NO HACE
--------------------------

**No elige la politica.** Es una decision comercial de Sizu, no del codigo.
Sin politica configurada no hay cotizacion: ``cotizar()`` levanta. Un motor
con politica por omision acabaria vendiendo bajo una regla que nadie decidio.

**No inventa la comision del proveedor.** TAECEL no la publica ("preguntanos
por el porcentaje"). Cuando no se sabe, el margen es ``None`` -- no cero, no
estimado -- y la cotizacion lo dice. Un margen inventado es peor que ningun
margen: da confianza falsa sobre si el negocio gana o pierde.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Final

__all__ = [
    "MetodoPago",
    "PoliticaPrecio",
    "MecanismoComision",
    "TarifaPasarela",
    "ComisionProveedor",
    "Cotizacion",
    "PrecioError",
    "MetodoDePagoNoPermitido",
    "ConfiguracionDePrecioInvalida",
    "CONEKTA_TARJETA",
    "TAECEL_BONO_6",
    "cotizar",
    "cuota_minima_uniforme",
    "saldo_por_fondeo",
    "efectivo_para_saldo",
]

#: Un punto base es 1/100 de 1%. 3.4% = 340 bp.
#:
#: Se usan enteros y no Decimal para los porcentajes porque una tarifa
#: contractual siempre viene en incrementos de centesima de punto, y un entero
#: no se puede escribir mal por redondeo al pasar de un lado a otro.
BASE_PUNTOS: Final[int] = 10_000


class MetodoPago(enum.StrEnum):
    EFECTIVO = "EFECTIVO"
    TARJETA = "TARJETA"


class PoliticaPrecio(enum.StrEnum):
    """Las tres salidas legitimas. No hay una cuarta que sea legal."""

    SOLO_EFECTIVO = "SOLO_EFECTIVO"
    CUOTA_UNIFORME = "CUOTA_UNIFORME"
    ABSORBER = "ABSORBER"


class MecanismoComision(enum.StrEnum):
    """COMO nos concede el proveedor su descuento. No es un detalle.

    El mismo porcentaje produce costos distintos segun el mecanismo, y la
    diferencia va siempre en la direccion peligrosa si se modela mal.

    ``DESCUENTO_POR_TRANSACCION``
        Cada recarga se cobra mas barata que su valor facial. Con 6%, una
        recarga de $100 nos cuesta $94.00.

    ``BONO_AL_FONDEAR``
        El descuento se entrega al comprar saldo, no al gastarlo: se fondea
        efectivo y se recibe MAS saldo. Es el mecanismo de TAECEL: fondear
        $5,000 deja $5,300 de saldo.

        Lo que se gasta en cada recarga es SALDO, y cada peso de saldo costo
        1/1.06 pesos de efectivo. Asi que una recarga de $100 cuesta
        100/1.06 = **$94.34**, no $94.00.

        Los 34 centavos de diferencia son pequenos y el error es sistematico:
        siempre hace creer que se gana mas. Con 6% el descuento efectivo no es
        600 puntos base sino 566.
    """

    DESCUENTO_POR_TRANSACCION = "DESCUENTO_POR_TRANSACCION"
    BONO_AL_FONDEAR = "BONO_AL_FONDEAR"


class PrecioError(Exception):
    """Base de los errores de este modulo."""


class MetodoDePagoNoPermitido(PrecioError):
    """La politica vigente no admite ese metodo para ese producto."""


class ConfiguracionDePrecioInvalida(PrecioError):
    """Falta configuracion, o la que hay se contradice."""


def _techo(numerador: int, denominador: int) -> int:
    """Division entera redondeando HACIA ARRIBA.

    Todos los COSTOS se redondean hacia arriba. Subestimar un costo que
    alguien mas nos va a cobrar es como un margen se vuelve negativo sin que
    nadie lo note: el centavo que falta no aparece en ningun reporte, aparece
    en el estado de cuenta.
    """
    if denominador <= 0:  # pragma: no cover - guardia
        raise ValueError("Denominador invalido.")
    return -(-numerador // denominador)


@dataclass(frozen=True, slots=True)
class TarifaPasarela:
    """Lo que la pasarela cobra por aceptar un pago.

    Son datos del contrato, no estimaciones. Se pasan explicitamente para que
    cambiar de pasarela o renegociar la tarifa sea cambiar un valor y no
    buscar porcentajes incrustados en el codigo.
    """

    #: Porcentaje sobre el monto cobrado, en puntos base. 3.4% = 340.
    porcentaje_bp: int
    #: Cuota fija por transaccion, en centavos. $3.00 = 300.
    fija_cents: int
    #: IVA sobre la comision, en puntos base. 16% = 1600.
    iva_bp: int = 1600
    etiqueta: str = ""

    def __post_init__(self) -> None:
        for nombre, valor in (
            ("porcentaje_bp", self.porcentaje_bp),
            ("fija_cents", self.fija_cents),
            ("iva_bp", self.iva_bp),
        ):
            if not isinstance(valor, int) or isinstance(valor, bool):
                raise ConfiguracionDePrecioInvalida(
                    f"TarifaPasarela.{nombre} debe ser int (nunca float: es dinero)."
                )
            if valor < 0:
                raise ConfiguracionDePrecioInvalida(
                    f"TarifaPasarela.{nombre} no puede ser negativo."
                )
        if self.porcentaje_bp >= BASE_PUNTOS:
            raise ConfiguracionDePrecioInvalida(
                "Una comision de 100% o mas no tiene solucion: no existe cuota "
                "que alcance el punto de equilibrio."
            )

    def costo(self, monto_cobrado_cents: int) -> int:
        """Comision total, con IVA, sobre el monto REALMENTE cobrado.

        Ojo con el detalle que se olvida: la pasarela cobra sobre el total que
        pasa por ella, **cuota de servicio incluida**. La cuota tambien paga
        comision. Calcular la comision sobre el precio de lista deja el margen
        corto en cada venta.
        """
        if monto_cobrado_cents < 0:
            raise ConfiguracionDePrecioInvalida("El monto cobrado no puede ser negativo.")
        variable = _techo(monto_cobrado_cents * self.porcentaje_bp, BASE_PUNTOS)
        subtotal = variable + self.fija_cents
        iva = _techo(subtotal * self.iva_bp, BASE_PUNTOS)
        return subtotal + iva


#: Tarifa publicada de Conekta para tarjeta (verificada septiembre 2026):
#: 3.4% + $3.00 MXN + IVA.
#:
#: Nota sobre el centavo: con estos numeros una recarga de $100 cuesta 743
#: centavos aqui, y la tarifa "de calculadora" da $7.424. La diferencia es que
#: este modulo redondea los costos hacia arriba. Es deliberado y es la
#: direccion segura: preferimos sobrestimar por un centavo lo que nos van a
#: cobrar que descubrir el faltante en el estado de cuenta.
CONEKTA_TARJETA: Final[TarifaPasarela] = TarifaPasarela(
    porcentaje_bp=340, fija_cents=300, iva_bp=1600, etiqueta="Conekta tarjeta 3.4%+$3+IVA"
)

#: Cobrar en efectivo en el mostrador no pasa por ninguna pasarela.
SIN_PASARELA: Final[TarifaPasarela] = TarifaPasarela(
    porcentaje_bp=0, fija_cents=0, iva_bp=0, etiqueta="Efectivo en mostrador"
)


@dataclass(frozen=True, slots=True)
class ComisionProveedor:
    """Lo que el proveedor nos concede, y **como** nos lo concede.

    El mecanismo importa tanto como el porcentaje, porque los dos producen
    costos distintos con el mismo numero. Ver ``MecanismoComision``.

    ``bp=None`` significa **no se sabe**. No se rellena con un numero
    plausible: el margen entonces es ``None`` y quien lea la cotizacion ve que
    no se sabe, en vez de creerse una estimacion.
    """

    bp: int | None
    fuente: str = ""
    mecanismo: "MecanismoComision" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.mecanismo is None:
            object.__setattr__(
                self, "mecanismo", MecanismoComision.DESCUENTO_POR_TRANSACCION
            )
        if self.bp is None:
            return
        if not isinstance(self.bp, int) or isinstance(self.bp, bool):
            raise ConfiguracionDePrecioInvalida(
                "ComisionProveedor.bp debe ser int en puntos base, o None."
            )
        if self.bp < 0:
            raise ConfiguracionDePrecioInvalida(
                "La comision del proveedor no puede ser negativa."
            )
        # Un descuento del 100% significaria coste cero, que no existe. Un
        # BONO del 100% si es concebible (fondear $100 y recibir $200), asi
        # que el limite solo aplica al mecanismo de descuento.
        if (
            self.mecanismo is MecanismoComision.DESCUENTO_POR_TRANSACCION
            and self.bp >= BASE_PUNTOS
        ):
            raise ConfiguracionDePrecioInvalida(
                "Un descuento por transaccion del 100% o mas daria costo cero."
            )

    @property
    def conocida(self) -> bool:
        return self.bp is not None

    def costo(self, valor_facial_cents: int) -> int | None:
        """Lo que nos cuesta entregar esa recarga. ``None`` si no se sabe.

        Los dos mecanismos se calculan distinto y la diferencia es dinero:

        ``DESCUENTO_POR_TRANSACCION``
            ``costo = facial - facial * bp``

        ``BONO_AL_FONDEAR``
            ``costo = facial / (1 + bp)``

            Porque lo que se gasta es SALDO, y ese saldo se compro mas barato.
            Con 6% de bono, $5,000 de efectivo producen $5,300 de saldo; cada
            peso de saldo costo 1/1.06 pesos de efectivo.

        El costo se redondea hacia ARRIBA en los dos casos: si hay que
        equivocarse, que sea contra nosotros y no contra la caja.
        """
        if self.bp is None:
            return None

        if self.mecanismo is MecanismoComision.BONO_AL_FONDEAR:
            return _techo(valor_facial_cents * BASE_PUNTOS, BASE_PUNTOS + self.bp)

        descuento = (valor_facial_cents * self.bp) // BASE_PUNTOS
        return valor_facial_cents - descuento

    def descuento_efectivo_bp(self, valor_facial_cents: int = 10_000) -> int | None:
        """Descuento REAL que resulta, en puntos base, sobre un facial dado.

        Existe para poder mirar de frente la trampa del bono: 600 puntos de
        bono al fondear no son 600 puntos de descuento, son 566. Sin esta
        funcion la unica forma de ver la diferencia seria calcularla a mano
        cada vez que alguien la pusiera en duda.
        """
        costo = self.costo(valor_facial_cents)
        if costo is None:
            return None
        return ((valor_facial_cents - costo) * BASE_PUNTOS) // valor_facial_cents


#: TAECEL: 6% de BONO AL FONDEAR. Confirmado por su soporte (septiembre 2026).
#:
#: Ejemplo que ellos mismos dieron: fondear $5,000 MXN deja $5,300 MXN de
#: saldo en la Bolsa de Tiempo Aire. Aplica a Telcel, Movistar, AT&T, Unefon y
#: a Telcel Amigo Sin Limite de $100 y $200.
#:
#: LA TRAMPA: 6% de bono NO es 6% de descuento. El descuento efectivo es
#: 1 - 1/1.06 = **5.66%**. Sobre una recarga de $100 la diferencia entre
#: modelarlo bien y mal es de 34 centavos, y va en la direccion peligrosa:
#: el modelo equivocado hace creer que se gana mas de lo que se gana.
TAECEL_BONO_6: Final[ComisionProveedor] = ComisionProveedor(
    bp=600,
    fuente="TAECEL, confirmado por soporte: 6% de bono al fondear",
    mecanismo=MecanismoComision.BONO_AL_FONDEAR,
)


@dataclass(frozen=True, slots=True)
class Cotizacion:
    """Lo que se cobra, lo que cuesta, y lo que queda.

    Es inmutable y lleva todos los componentes, no solo el total. Un
    comprobante que dice "$102.53" sin decir que $100.00 es la recarga y $2.53
    la cuota de servicio no le sirve ni al cliente ni a quien concilie.
    """

    #: Valor facial del producto. Lo que el cliente recibe.
    precio_lista_cents: int
    #: Cuota de servicio. IDENTICA en todos los metodos de pago, por diseno.
    cuota_servicio_cents: int
    #: Lo que el cliente paga.
    total_cents: int
    #: Lo que nos cobra la pasarela por este cobro.
    costo_pasarela_cents: int
    #: Lo que nos cuesta entregar la recarga. ``None`` = no se sabe.
    costo_proveedor_cents: int | None
    metodo: MetodoPago
    politica: PoliticaPrecio
    moneda: str = "MXN"
    #: Cosas que una persona tiene que leer antes de vender esto.
    advertencias: tuple[str, ...] = field(default_factory=tuple)

    @property
    def margen_conocido(self) -> bool:
        return self.costo_proveedor_cents is not None

    @property
    def margen_cents(self) -> int | None:
        """Lo que queda despues de pagar pasarela y proveedor.

        ``None`` cuando falta un costo. No es cero: cero significaria "no
        ganamos ni perdemos", que es una afirmacion, y aqui no hay ninguna.
        """
        if self.costo_proveedor_cents is None:
            return None
        return self.total_cents - self.costo_pasarela_cents - self.costo_proveedor_cents

    @property
    def es_perdida(self) -> bool | None:
        margen = self.margen_cents
        return None if margen is None else margen < 0

    @property
    def vendible(self) -> bool:
        """Si esta cotizacion se puede cobrar.

        Las tres reglas, y cada una responde a un caso distinto:

        * Margen desconocido -> **si** se puede vender. Bloquear aqui dejaria
          el negocio parado indefinidamente esperando que TAECEL publique su
          comision, que no va a pasar. La advertencia queda escrita.
        * Perdida bajo ``ABSORBER`` -> **si**. Es exactamente lo que esa
          politica significa; alguien lo decidio a proposito.
        * Perdida bajo ``CUOTA_UNIFORME`` -> **no**. Ahi la perdida no es una
          decision sino una cuota mal calculada, y venderla es perder dinero
          creyendo que se gana. ``cuota_minima_uniforme()`` da el numero que
          la arregla.
        """
        if self.es_perdida and self.politica is PoliticaPrecio.CUOTA_UNIFORME:
            return False
        return True


def cotizar(
    *,
    precio_lista_cents: int,
    metodo: MetodoPago,
    politica: PoliticaPrecio,
    cuota_servicio_cents: int = 0,
    tarifa: TarifaPasarela | None = None,
    comision_proveedor: ComisionProveedor,
    moneda: str = "MXN",
) -> Cotizacion:
    """Calcula el precio de una venta.

    Fijate en lo que NO esta en esta firma: no hay forma de pasar una cuota
    distinta segun el metodo de pago. La cuota es un parametro del producto,
    no del metodo. Esa ausencia es la que hace estructuralmente imposible
    construir un recargo por tarjeta, que es lo que los Terminos de Conekta
    prohiben. Una validacion se puede olvidar; un parametro que no existe, no.

    ``tarifa`` puede omitirse solo para efectivo, que no pasa por pasarela.
    """
    if not isinstance(precio_lista_cents, int) or isinstance(precio_lista_cents, bool):
        raise ConfiguracionDePrecioInvalida(
            "precio_lista_cents debe ser int (centavos). Nunca float: es dinero."
        )
    if precio_lista_cents <= 0:
        raise ConfiguracionDePrecioInvalida("El precio de lista tiene que ser positivo.")
    if not isinstance(cuota_servicio_cents, int) or isinstance(cuota_servicio_cents, bool):
        raise ConfiguracionDePrecioInvalida("cuota_servicio_cents debe ser int (centavos).")
    if cuota_servicio_cents < 0:
        raise ConfiguracionDePrecioInvalida("La cuota de servicio no puede ser negativa.")

    advertencias: list[str] = []

    # --- la politica decide antes que nada -------------------------------
    if politica is PoliticaPrecio.SOLO_EFECTIVO and metodo is not MetodoPago.EFECTIVO:
        raise MetodoDePagoNoPermitido(
            "La politica vigente para este producto es SOLO_EFECTIVO: no se "
            "acepta tarjeta. No se cobra mas por tarjeta; simplemente no se "
            "vende por ese medio."
        )

    if politica is PoliticaPrecio.CUOTA_UNIFORME and cuota_servicio_cents == 0:
        raise ConfiguracionDePrecioInvalida(
            "La politica es CUOTA_UNIFORME pero la cuota es cero. Define la "
            "cuota o cambia de politica: una cuota uniforme de cero es la "
            "politica ABSORBER con otro nombre, y conviene que se llame por "
            "el suyo para que el margen negativo no sorprenda a nadie."
        )

    if politica is PoliticaPrecio.ABSORBER and cuota_servicio_cents != 0:
        raise ConfiguracionDePrecioInvalida(
            "La politica es ABSORBER pero hay una cuota de servicio. "
            "Absorber significa que el negocio paga el costo, no el cliente."
        )

    cobrada = cuota_servicio_cents if politica is PoliticaPrecio.CUOTA_UNIFORME else 0
    total = precio_lista_cents + cobrada

    # --- costos -----------------------------------------------------------
    if metodo is MetodoPago.EFECTIVO:
        efectiva = SIN_PASARELA
    else:
        if tarifa is None:
            raise ConfiguracionDePrecioInvalida(
                "Falta la tarifa de la pasarela para un cobro con tarjeta. No "
                "se asume ninguna: una tarifa supuesta produce un margen "
                "supuesto."
            )
        efectiva = tarifa

    costo_pasarela = efectiva.costo(total)
    costo_proveedor = comision_proveedor.costo(precio_lista_cents)

    # --- advertencias -----------------------------------------------------
    if costo_proveedor is None:
        advertencias.append(
            "Margen DESCONOCIDO: no se sabe la comision del proveedor"
            + (f" ({comision_proveedor.fuente})" if comision_proveedor.fuente else "")
            + ". No se estima."
        )
    else:
        margen = total - costo_pasarela - costo_proveedor
        if margen < 0 and politica is PoliticaPrecio.CUOTA_UNIFORME:
            minima = cuota_minima_uniforme(
                precio_lista_cents=precio_lista_cents,
                tarifa=tarifa or SIN_PASARELA,
                comision_proveedor=comision_proveedor,
            )
            advertencias.append(
                f"PERDIDA de {margen} centavos por venta: la cuota de "
                f"{cuota_servicio_cents} no cubre los costos. Cuota minima para "
                f"no perder: {minima} centavos. No se vende asi."
            )
        elif margen < 0:
            advertencias.append(
                f"Perdida DELIBERADA de {abs(margen)} centavos por venta "
                f"(politica {politica})."
            )

    return Cotizacion(
        precio_lista_cents=precio_lista_cents,
        cuota_servicio_cents=cobrada,
        total_cents=total,
        costo_pasarela_cents=costo_pasarela,
        costo_proveedor_cents=costo_proveedor,
        metodo=metodo,
        politica=politica,
        moneda=moneda,
        advertencias=tuple(advertencias),
    )


def cuota_minima_uniforme(
    *,
    precio_lista_cents: int,
    tarifa: TarifaPasarela,
    comision_proveedor: ComisionProveedor,
) -> int | None:
    """La cuota uniforme mas baja con la que la venta no pierde dinero.

    Es el numero que hace accionable la politica ``CUOTA_UNIFORME``: sin el,
    elegir la cuota es adivinar.

    Tiene una vuelta que no es obvia: **la cuota tambien paga comision.**
    Subirla un peso no deja un peso mas de margen, porque la pasarela cobra su
    porcentaje tambien sobre ese peso. Por eso no basta con sumar el costo; hay
    que resolver

        T - (T*p + f)*(1+iva) - C = 0

    donde ``T`` es el total cobrado y ``C`` lo que nos cuesta la recarga.

    El resultado algebraico se usa solo como punto de partida y despues se
    **verifica contra la funcion de costo real**, la misma que usa
    ``cotizar()``, subiendo de centavo en centavo hasta que el margen deja de
    ser negativo. La razon es que ``TarifaPasarela.costo()`` redondea hacia
    arriba en dos pasos, asi que la solucion exacta del algebra puede quedarse
    corta por un centavo. Un "minimo" que al cobrarlo sigue perdiendo no
    serviria de nada.

    Devuelve ``None`` cuando no se conoce la comision del proveedor: sin ese
    dato no hay punto de equilibrio que calcular, y un numero inventado aqui
    seria el que alguien usara para fijar precios de verdad.
    """
    costo_proveedor = comision_proveedor.costo(precio_lista_cents)
    if costo_proveedor is None:
        return None

    p = Decimal(tarifa.porcentaje_bp) / Decimal(BASE_PUNTOS)
    f = Decimal(tarifa.fija_cents)
    iva = Decimal(1) + Decimal(tarifa.iva_bp) / Decimal(BASE_PUNTOS)

    denominador = Decimal(1) - p * iva
    if denominador <= 0:  # pragma: no cover - lo impide TarifaPasarela
        raise ConfiguracionDePrecioInvalida(
            "La comision porcentual se come el 100% del cobro: no hay cuota "
            "que alcance el equilibrio."
        )

    total_estimado = (f * iva + Decimal(costo_proveedor)) / denominador
    semilla = int(total_estimado.to_integral_value(rounding=ROUND_CEILING)) - precio_lista_cents
    cuota = max(0, semilla - 2)  # se arranca un poco por debajo y se sube

    # Verificacion contra la funcion de costo real. El tope es defensivo: con
    # una tarifa valida la convergencia es de un par de centavos.
    for _ in range(10_000):
        total = precio_lista_cents + cuota
        if total - tarifa.costo(total) - costo_proveedor >= 0:
            return cuota
        cuota += 1

    raise ConfiguracionDePrecioInvalida(  # pragma: no cover - defensivo
        "No se encontro una cuota de equilibrio en un rango razonable. "
        "Revisa la tarifa de la pasarela y la comision del proveedor."
    )


# ---------------------------------------------------------------------------
# Fondeo: la otra cara del bono
# ---------------------------------------------------------------------------
# Estas dos funciones existen para poder planear el fondeo sin hacer cuentas a
# mano, y para que la relacion efectivo <-> saldo este escrita una sola vez.


def saldo_por_fondeo(efectivo_cents: int, comision: ComisionProveedor) -> int | None:
    """Saldo que se obtiene al fondear ese efectivo. ``None`` si no se sabe.

    Con el mecanismo de bono: ``saldo = efectivo * (1 + bp)``. Fondear $5,000
    con 6% deja $5,300, que es exactamente el ejemplo que dio TAECEL.

    Con descuento por transaccion el fondeo es uno a uno: el descuento llega
    despues, al gastar.

    Se redondea hacia ABAJO. El bono que promete el proveedor se cobra cuando
    llega, no antes: sobrestimarlo aqui haria planear un piloto con saldo que
    quiza no exista.
    """
    if efectivo_cents < 0:
        raise ValueError("El efectivo fondeado no puede ser negativo.")
    if comision.bp is None:
        return None
    if comision.mecanismo is MecanismoComision.BONO_AL_FONDEAR:
        return efectivo_cents + (efectivo_cents * comision.bp) // BASE_PUNTOS
    return efectivo_cents


def efectivo_para_saldo(saldo_cents: int, comision: ComisionProveedor) -> int | None:
    """Efectivo necesario para obtener ese saldo. ``None`` si no se sabe.

    La inversa de ``saldo_por_fondeo``, redondeando hacia ARRIBA: hay que
    poner el peso completo aunque la division no sea exacta.
    """
    if saldo_cents < 0:
        raise ValueError("El saldo objetivo no puede ser negativo.")
    if comision.bp is None:
        return None
    if comision.mecanismo is MecanismoComision.BONO_AL_FONDEAR:
        return _techo(saldo_cents * BASE_PUNTOS, BASE_PUNTOS + comision.bp)
    return saldo_cents
