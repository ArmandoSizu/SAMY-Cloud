"""Traduccion de las respuestas de Linntae a tipos de SAMY Cloud.

Vive separado del cliente HTTP a proposito: es la parte que se puede probar
entera sin red, y es donde estan los dos riesgos silenciosos de esta
integracion.

RIESGO 1: LINNTAE DEVUELVE EL DINERO COMO TEXTO
-----------------------------------------------

Su consulta de saldo responde:

    {"code": 0, "plataforma": "$3,314.00", "comision": "$96.95"}

``float("$3,314.00")`` levanta. Y lo que uno escribe cuando levanta es
``float(crudo.replace("$","").replace(",",""))``, que **no** levanta y que es
exactamente el error que este proyecto prohibe: a partir de ahi el saldo es un
binario inexacto. Aqui todo pasa por ``Decimal`` y termina en centavos
enteros.

Lo que no se entiende devuelve ``None``, nunca cero. Cero es una afirmacion
("no hay saldo") y ``None`` es la ausencia de una. Confundirlos bloquea ventas
de una cuenta con fondos, o -peor- deja pasar ventas creyendo que los hay.

RIESGO 2: LA ESPECIFICACION SE CONTRADICE CONSIGO MISMA
-------------------------------------------------------

En dos endpoints el *ejemplo* y el *esquema* declaran formas distintas, y no
se puede saber cual manda hasta llamar a DEMO:

``GET /config/syncProducts``
    El ejemplo devuelve ``products`` como una LISTA de objetos de una sola
    llave: ``[{"TIEMPO AIRE": [...]}, {"RECARGA VIRTUAL": [...]}]``.
    El esquema lo declara como un OBJETO con llaves fijas:
    ``{"TIEMPO AIRE": [...], "VIRTUALES": [...]}``.
    Ademas la seccion de virtuales se llama ``RECARGA VIRTUAL`` en el ejemplo
    y ``VIRTUALES`` en el esquema.

``GET /config/getProductsCommissions``
    El ejemplo devuelve ``data`` plano: ``[{"comision":"5.5%","id":1,
    "nombre":"TELCEL"}]``. El esquema lo declara agrupado:
    ``[{"key":...,"title":...,"list":[{"comision":...,"sku":...}]}]``.

Este modulo acepta **las dos formas** y, cuando no reconoce ninguna, lo dice
en vez de devolver una lista vacia. Una lista vacia parece "el proveedor no
tiene productos" y llevaria a desactivar catalogo; "no entendi la respuesta"
lleva a mirarla.

Y por eso las secciones se clasifican por la FORMA de sus filas y no por su
nombre: una compania trae ``idOperator`` y ``offers``; un servicio, un pin o
un peaje traen ``sku``. Eso no depende de como Linntae decida llamar a la
seccion manana.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from samy_common.money import Money

__all__ = [
    "MONEDA",
    "AlcanceComision",
    "TasaComision",
    "OfertaLinntae",
    "CompaniaLinntae",
    "ProductoConSku",
    "ComisionLinntae",
    "SaldosLinntae",
    "CatalogoLinntae",
    "RespuestaIlegible",
    "a_money",
    "a_tasa",
    "leer_saldos",
    "leer_companias",
    "leer_catalogo",
    "leer_comisiones",
    "leer_ventas",
]

MONEDA: Final[str] = "MXN"

#: Lo que se quita de una cifra antes de interpretarla: signo de pesos,
#: separadores de miles, espacios normales y el espacio duro que a veces
#: aparece en respuestas generadas por plantillas, y las siglas de moneda.
_BASURA: Final[re.Pattern[str]] = re.compile(r"[$\s  ,]|MXN|MN|M\.N\.", re.IGNORECASE)
_SOLO_NUMERO: Final[re.Pattern[str]] = re.compile(r"^-?\d+(?:\.\d+)?$")


class RespuestaIlegible(ValueError):
    """La respuesta de Linntae no tiene ninguna de las formas documentadas.

    Es distinta de "respuesta vacia" y por eso es una excepcion y no una
    lista sin elementos. Una lista vacia se propaga como "el proveedor no
    ofrece nada" y termina desactivando catalogo; esto se propaga como "hay
    que mirar la respuesta".
    """


# ---------------------------------------------------------------------------
# Dinero y porcentajes
# ---------------------------------------------------------------------------


def a_money(crudo: Any, moneda: str = MONEDA) -> Money | None:
    """``"$3,314.00"`` -> ``Money(331400)``. ``None`` si no se entiende.

    Acepta las formas que aparecen en la especificacion y las que produce
    cualquier plantilla razonable: ``"$3,314.00"``, ``"3314"``, ``"3,314.5"``,
    un entero, un ``Decimal``. Acepta negativos con signo y entre parentesis,
    que es como se escribe un cargo en un estado de cuenta.

    **Rechaza ``float`` explicitamente.** Si Linntae llegara a mandar el saldo
    como numero JSON, ``json.loads`` lo entrega como ``float`` y aceptarlo
    aqui reintroduciria el error de precision por la puerta de atras. Se
    convierte via ``repr``, que para un float de dinero da la cadena decimal
    mas corta que lo representa, y eso es lo mas fiel que se puede hacer con
    un dato que ya llego dañado. Queda registrado como sospechoso por quien
    llame.
    """
    if crudo is None or isinstance(crudo, bool):
        return None

    if isinstance(crudo, int):
        return Money(crudo * 100, moneda)

    if isinstance(crudo, Decimal):
        try:
            return Money.parse(crudo, moneda)
        except (ValueError, ArithmeticError, TypeError):
            return None

    if isinstance(crudo, float):
        texto = repr(crudo)
    else:
        texto = str(crudo)

    texto = texto.strip()
    if not texto:
        return None

    negativo = False
    if texto.startswith("(") and texto.endswith(")"):
        negativo = True
        texto = texto[1:-1]

    limpio = _BASURA.sub("", texto)
    if limpio.startswith("-"):
        negativo = True
        limpio = limpio[1:]
    elif limpio.startswith("+"):
        limpio = limpio[1:]

    if not limpio or not _SOLO_NUMERO.match(limpio):
        return None

    try:
        valor = Decimal(limpio)
    except InvalidOperation:
        return None
    if not valor.is_finite():
        return None

    try:
        dinero = Money.parse(-valor if negativo else valor, moneda)
    except (ValueError, ArithmeticError, TypeError):
        return None
    return dinero


class AlcanceComision(enum.StrEnum):
    """A que se le aplica la comision que Linntae reporta.

    Importa porque las dos formas de su respuesta identifican cosas
    distintas: la plana trae ``id``/``nombre``, que en el ejemplo son
    operadores (``id: 1, nombre: "TELCEL"``); la agrupada trae ``sku``, que es
    un producto. Guardar ambas bajo una sola etiqueta haria creer que la
    comision de un operador aplica a un SKU concreto, o al contrario.
    """

    OPERADOR = "OPERADOR"
    SKU = "SKU"
    #: Vino en la respuesta pero no se pudo decidir a que se refiere.
    INDETERMINADO = "INDETERMINADO"


@dataclass(frozen=True, slots=True)
class TasaComision:
    """Una tasa de comision leida de Linntae, con su fidelidad declarada.

    ``bps`` es lo que consume el motor de precios, que trabaja en puntos base
    enteros. ``exacta_en_bps`` dice si esa conversion fue exacta: 5.5% son 550
    puntos base sin perder nada, pero un hipotetico 5.555% no cabe en un
    entero de puntos base.

    Cuando no cabe se trunca **hacia abajo**, nunca al mas cercano. Una
    comision es un ingreso nuestro, y la regla del proyecto es que los
    ingresos se subestiman y los costos se sobrestiman: si hay que
    equivocarse, que sea contra nosotros y no contra la caja. Redondear
    5.555% a 556 puntos base haria creer que se gana medio punto base mas de
    lo que el proveedor concede, en todas y cada una de las ventas.

    Y se declara en ``exacta_en_bps`` en vez de truncar en silencio, porque
    el que lea ``bps`` tiene derecho a saber si esta viendo la tasa del
    contrato o una aproximacion.
    """

    bps: int
    tasa_pct: Decimal
    exacta_en_bps: bool
    crudo: str = ""

    def __str__(self) -> str:
        return f"{self.tasa_pct}%"


def a_tasa(crudo: Any) -> TasaComision | None:
    """``"5.5%"`` -> ``TasaComision(bps=550)``. ``None`` si no se entiende.

    Un numero sin ``%`` se interpreta como porcentaje, que es lo que Linntae
    hace en su propio esquema (``registerPdv.comision`` tiene ``default: 5`` y
    se documenta como "Porcentaje de comision"). Lo que NO se hace es
    interpretar ``0.055`` como 5.5%: seria adivinar entre dos lecturas
    legitimas del mismo numero, y la equivocada cambia el margen cien veces.

    Se rechaza fuera de ``[0, 100]``: una comision negativa o de mas del 100%
    no es una tasa mal escrita, es una respuesta que no se entendio.
    """
    if crudo is None or isinstance(crudo, bool):
        return None

    texto = repr(crudo) if isinstance(crudo, float) else str(crudo)
    texto = texto.strip()
    if not texto:
        return None

    limpio = texto.replace("%", "").replace(" ", "").replace(" ", "").replace(",", "")
    if not limpio or not _SOLO_NUMERO.match(limpio):
        return None

    try:
        pct = Decimal(limpio)
    except InvalidOperation:
        return None
    if not pct.is_finite() or pct < 0 or pct > 100:
        return None

    en_bps = pct * 100
    # ``int()`` sobre un Decimal positivo trunca hacia cero, que aqui es
    # hacia abajo. Deliberado: ver el docstring de TasaComision.
    truncado = int(en_bps)
    return TasaComision(
        bps=truncado,
        tasa_pct=pct,
        exacta_en_bps=en_bps == truncado,
        crudo=texto,
    )


# ---------------------------------------------------------------------------
# Saldo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SaldosLinntae:
    """Las tres bolsas que Linntae declara. Cada una puede faltar.

    Se guardan separadas y NO se suman. Sumarlas daria un numero mayor que el
    que realmente se puede gastar en una recarga: la recarga sale de
    ``plataforma``, y que la bolsa de comision sea liquida es una pregunta
    que la especificacion no contesta.
    """

    plataforma: Money | None
    comision: Money | None = None
    servicios: Money | None = None
    #: Los textos tal como llegaron. Es la evidencia de lo que se interpreto.
    crudos: dict[str, str] = field(default_factory=dict)

    @property
    def hay_plataforma(self) -> bool:
        return self.plataforma is not None


def leer_saldos(payload: dict[str, Any]) -> SaldosLinntae:
    """Interpreta ``/balance/getBalance``.

    Nota sobre lo que NO hace: no falla si una bolsa falta o es ilegible. La
    que importa para vender es ``plataforma``, y quien decide es la guarda de
    saldo, que ya sabe tratar "no lo se".
    """
    crudos = {
        nombre: str(payload.get(nombre))
        for nombre in ("plataforma", "comision", "servicios")
        if payload.get(nombre) is not None
    }
    return SaldosLinntae(
        plataforma=a_money(payload.get("plataforma")),
        comision=a_money(payload.get("comision")),
        servicios=a_money(payload.get("servicios")),
        crudos=crudos,
    )


# ---------------------------------------------------------------------------
# Catalogo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfertaLinntae:
    """Una denominacion concreta. ``id_offer`` es lo que viaja en la recarga."""

    id_offer: int
    monto: Money | None
    descripcion: str = ""
    categoria: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompaniaLinntae:
    """Un operador de Linntae con sus ofertas."""

    id_operator: int
    nombre: str
    seccion: str = ""
    logo_url: str = ""
    ofertas: tuple[OfertaLinntae, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ProductoConSku:
    """Servicios, pines y peajes. Se leen y se guardan; no se venden aun."""

    sku: int
    nombre: str
    seccion: str = ""
    monto: Money | None = None
    #: ``amoutProvider`` (asi, con la errata de Linntae): lo que el proveedor
    #: cobra por procesar la operacion. No es nuestra comision.
    costo_proveedor: Money | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CatalogoLinntae:
    """El catalogo tecnico completo, separado por forma de fila."""

    companias: tuple[CompaniaLinntae, ...] = field(default_factory=tuple)
    con_sku: tuple[ProductoConSku, ...] = field(default_factory=tuple)
    #: Secciones que llegaron y no se supieron clasificar. Se reportan.
    secciones_desconocidas: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_ofertas(self) -> int:
        return sum(len(c.ofertas) for c in self.companias)


def _entero(crudo: Any) -> int | None:
    if crudo is None or isinstance(crudo, bool):
        return None
    try:
        return int(str(crudo).strip())
    except (TypeError, ValueError):
        return None


def _oferta(crudo: dict[str, Any]) -> OfertaLinntae | None:
    id_offer = _entero(crudo.get("idOffer"))
    if id_offer is None:
        # Sin idOffer no hay nada que mandarle a Linntae. Una oferta sin
        # identificador no es una oferta incompleta: es inejecutable.
        return None
    categoria = crudo.get("category")
    nombre_categoria = ""
    if isinstance(categoria, dict):
        nombre_categoria = str(categoria.get("name") or "").strip()
    return OfertaLinntae(
        id_offer=id_offer,
        monto=a_money(crudo.get("amount")),
        descripcion=str(crudo.get("description") or "").strip(),
        categoria=nombre_categoria,
        raw=dict(crudo),
    )


def _compania(crudo: dict[str, Any], seccion: str) -> CompaniaLinntae | None:
    id_operator = _entero(crudo.get("idOperator"))
    nombre = str(crudo.get("name") or "").strip()
    if id_operator is None or not nombre:
        return None
    ofertas = tuple(
        oferta
        for oferta in (
            _oferta(o) for o in crudo.get("offers") or [] if isinstance(o, dict)
        )
        if oferta is not None
    )
    return CompaniaLinntae(
        id_operator=id_operator,
        nombre=nombre,
        seccion=seccion,
        logo_url=str(crudo.get("imageUrl") or "").strip(),
        ofertas=ofertas,
    )


def _con_sku(crudo: dict[str, Any], seccion: str) -> ProductoConSku | None:
    sku = _entero(crudo.get("sku"))
    nombre = str(crudo.get("name") or "").strip()
    if sku is None or not nombre:
        return None
    return ProductoConSku(
        sku=sku,
        nombre=nombre,
        seccion=seccion,
        monto=a_money(crudo.get("amount")),
        # "amoutProvider" esta escrito asi en la especificacion de Linntae.
        # No se corrige: es el nombre real de la llave.
        costo_proveedor=a_money(crudo.get("amoutProvider")),
        raw=dict(crudo),
    )


def leer_companias(payload: dict[str, Any], *, seccion: str) -> list[CompaniaLinntae]:
    """Interpreta ``/products/taeCompanies`` y ``/products/taeVirtualCompanies``."""
    crudas = payload.get("companies")
    if not isinstance(crudas, list):
        raise RespuestaIlegible(
            "Linntae no devolvio la lista 'companies' que declara su "
            f"especificacion. Llaves recibidas: {sorted(payload)}."
        )
    companias = [
        c
        for c in (_compania(x, seccion) for x in crudas if isinstance(x, dict))
        if c is not None
    ]
    return companias


def _secciones(crudo: Any) -> list[tuple[str, list[Any]]]:
    """Normaliza ``products`` a una lista de (nombre de seccion, filas).

    Acepta las dos formas que la especificacion declara (ver el docstring del
    modulo) y ademas el caso mixto, que es el que aparece cuando alguien
    cambia una y no la otra.
    """
    secciones: list[tuple[str, list[Any]]] = []

    if isinstance(crudo, dict):
        for nombre, filas in crudo.items():
            if isinstance(filas, list):
                secciones.append((str(nombre), filas))
        return secciones

    if isinstance(crudo, list):
        for entrada in crudo:
            if isinstance(entrada, dict):
                for nombre, filas in entrada.items():
                    if isinstance(filas, list):
                        secciones.append((str(nombre), filas))
        return secciones

    raise RespuestaIlegible(
        "El campo 'products' de Linntae no es ni objeto ni lista: "
        f"{type(crudo).__name__}."
    )


def leer_catalogo(payload: dict[str, Any]) -> CatalogoLinntae:
    """Interpreta ``/config/syncProducts``.

    Clasifica cada seccion por la forma de sus filas, no por su nombre. Ver
    el docstring del modulo: la propia especificacion llama a la misma
    seccion ``RECARGA VIRTUAL`` en un sitio y ``VIRTUALES`` en otro.
    """
    if "products" not in payload:
        raise RespuestaIlegible(
            "Linntae no devolvio 'products'. Llaves recibidas: "
            f"{sorted(payload)}."
        )

    companias: list[CompaniaLinntae] = []
    con_sku: list[ProductoConSku] = []
    desconocidas: list[str] = []

    for nombre, filas in _secciones(payload["products"]):
        dicts = [f for f in filas if isinstance(f, dict)]
        if not dicts:
            continue

        # La primera fila decide la forma de la seccion. Una compania trae
        # idOperator; un servicio, pin o peaje trae sku.
        muestra = dicts[0]
        if "idOperator" in muestra or "offers" in muestra:
            companias.extend(
                c for c in (_compania(f, nombre) for f in dicts) if c is not None
            )
        elif "sku" in muestra:
            con_sku.extend(
                p for p in (_con_sku(f, nombre) for f in dicts) if p is not None
            )
        else:
            desconocidas.append(nombre)

    if not companias and not con_sku:
        raise RespuestaIlegible(
            "Linntae devolvio 'products' pero no se reconocio ninguna fila. "
            f"Secciones vistas: {[n for n, _ in _secciones(payload['products'])]}."
        )

    return CatalogoLinntae(
        companias=tuple(companias),
        con_sku=tuple(con_sku),
        secciones_desconocidas=tuple(desconocidas),
    )


# ---------------------------------------------------------------------------
# Comisiones
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComisionLinntae:
    """Una comision tal como Linntae la reporta para NUESTRA cuenta.

    Esto es lo que sustituye a cualquier porcentaje escrito en el codigo. Los
    numeros que Linntae dio comercialmente (6% tradicionales, 5% virtuales)
    no aparecen en ninguna parte del repositorio: si la cuenta tiene otra
    tasa, la que manda es esta.
    """

    alcance: AlcanceComision
    clave: str
    nombre: str
    tasa: TasaComision | None
    categoria: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _comision_plana(crudo: dict[str, Any], categoria: str = "") -> ComisionLinntae | None:
    """Forma del EJEMPLO: ``{"comision":"5.5%","id":1,"nombre":"TELCEL"}``."""
    clave = crudo.get("id")
    nombre = str(crudo.get("nombre") or crudo.get("name") or "").strip()
    if clave is None and not nombre:
        return None
    return ComisionLinntae(
        alcance=AlcanceComision.OPERADOR,
        clave=str(clave if clave is not None else nombre),
        nombre=nombre,
        tasa=a_tasa(crudo.get("comision")),
        categoria=categoria,
        raw=dict(crudo),
    )


def _comision_con_sku(crudo: dict[str, Any], categoria: str) -> ComisionLinntae | None:
    """Forma del ESQUEMA: ``{"comision":"5.5%","sku":"...","name":"..."}``."""
    sku = crudo.get("sku")
    nombre = str(crudo.get("name") or crudo.get("nombre") or "").strip()
    if sku is None and not nombre:
        return None
    return ComisionLinntae(
        alcance=AlcanceComision.SKU if sku is not None else AlcanceComision.INDETERMINADO,
        clave=str(sku if sku is not None else nombre),
        nombre=nombre,
        tasa=a_tasa(crudo.get("comision")),
        categoria=categoria,
        raw=dict(crudo),
    )


def leer_comisiones(payload: dict[str, Any]) -> list[ComisionLinntae]:
    """Interpreta ``/config/getProductsCommissions`` en sus dos formas.

    Si una entrada trae ``list``, es la forma agrupada del esquema y sus
    elementos se identifican por ``sku``. Si no, es la forma plana del
    ejemplo y se identifican por ``id``. No se elige una: se detecta.
    """
    datos = payload.get("data")
    if not isinstance(datos, list):
        raise RespuestaIlegible(
            "Linntae no devolvio la lista 'data' de comisiones. Llaves "
            f"recibidas: {sorted(payload)}."
        )

    comisiones: list[ComisionLinntae] = []
    for entrada in datos:
        if not isinstance(entrada, dict):
            continue

        agrupada = entrada.get("list")
        if isinstance(agrupada, list):
            categoria = str(entrada.get("title") or entrada.get("key") or "").strip()
            for fila in agrupada:
                if isinstance(fila, dict):
                    leida = _comision_con_sku(fila, categoria)
                    if leida is not None:
                        comisiones.append(leida)
            continue

        if isinstance(agrupada, dict):
            # El esquema declara 'list' como objeto, no como array. Se acepta
            # tambien esa lectura literal.
            categoria = str(entrada.get("title") or entrada.get("key") or "").strip()
            leida = _comision_con_sku(agrupada, categoria)
            if leida is not None:
                comisiones.append(leida)
            continue

        leida = _comision_plana(entrada)
        if leida is not None:
            comisiones.append(leida)

    if not comisiones:
        raise RespuestaIlegible(
            "Linntae devolvio 'data' de comisiones pero no se reconocio "
            "ninguna entrada en las dos formas que declara su especificacion."
        )
    return comisiones


# ---------------------------------------------------------------------------
# Ventas
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VentaLinntae:
    """Una venta del historico de Linntae (``/sale/list``).

    ``id`` es el identificador unico de la venta. ``folio`` **no lo es**: en
    el propio ejemplo de la especificacion dos ventas distintas comparten el
    folio ``"12311057912"``. Tratar el folio como clave unica emparejaria
    operaciones que no son la misma, y eso en conciliacion significa cerrar
    una recarga con la evidencia de otra.
    """

    id_venta: int | None
    referencia: str
    monto: Money | None
    total: Money | None
    exitosa: bool | None
    nombre_producto: str = ""
    compania: str = ""
    folio: str = ""
    mensaje: str = ""
    registrada_en: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def leer_ventas(payload: dict[str, Any]) -> list[VentaLinntae]:
    """Interpreta ``/sale/list``.

    ``successTransaction`` se lee como ``None`` cuando no viene o no es
    booleano. No se asume ``False``: "no lo dijo" y "dijo que no" son cosas
    distintas y la conciliacion trata cada una de una forma.
    """
    lista = payload.get("list")
    if lista is None:
        raise RespuestaIlegible(
            f"Linntae no devolvio 'list' de ventas. Llaves: {sorted(payload)}."
        )
    if isinstance(lista, dict):
        lista = [lista]
    if not isinstance(lista, list):
        raise RespuestaIlegible(
            f"El campo 'list' de ventas no es una lista: {type(lista).__name__}."
        )

    ventas: list[VentaLinntae] = []
    for fila in lista:
        if not isinstance(fila, dict):
            continue
        exito = fila.get("successTransaction")
        ventas.append(
            VentaLinntae(
                id_venta=_entero(fila.get("id")),
                referencia=str(fila.get("reference") or "").strip(),
                monto=a_money(fila.get("amount")),
                total=a_money(fila.get("totalAmount")),
                exitosa=exito if isinstance(exito, bool) else None,
                nombre_producto=str(fila.get("productName") or "").strip(),
                compania=str(fila.get("carrierName") or "").strip(),
                folio=str(fila.get("folio") or "").strip(),
                mensaje=str(fila.get("message") or "").strip(),
                registrada_en=str(fila.get("registerDate") or "").strip(),
                raw=dict(fila),
            )
        )
    return ventas
