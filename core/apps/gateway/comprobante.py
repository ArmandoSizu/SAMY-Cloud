"""El comprobante, armado como lista blanca.

POR QUE UNA LISTA BLANCA Y NO PASAR LOS DICCIONARIOS A LA PLANTILLA
-------------------------------------------------------------------

La regla es que el comprobante NUNCA muestre numero de tarjeta, CVV, token,
llave de API, NIP, secretos ni identificadores tecnicos que no le sirven al
cliente.

Si la plantilla recibe el diccionario completo de la orden y el del
cumplimiento, esa regla depende de que nadie escriba nunca ``{{ order.algo }}``
de mas. Y eso ya paso: el comprobante terminaba imprimiendo ``{{ order.id }}``,
un UUID que al cliente no le dice nada y que ocupa una linea del ticket.

Con esta capa, la plantilla solo puede pintar lo que existe en
``Comprobante``. Agregar un campo sensible al comprobante exige agregarlo
aqui, a la vista, donde se nota al revisar. Hay una prueba que alimenta el
constructor con datos que CONTIENEN un token y un NIP y comprueba que no
aparecen en el resultado.

EL AMBIENTE SE MIRA POR LOS DOS LADOS
-------------------------------------

El cobro y el servicio pueden estar en ambientes distintos, y eso no es un
caso teorico: el efectivo en el mostrador es SIEMPRE real, mientras el
proveedor de recargas puede estar en sandbox. Ese comprobante no puede
parecer una operacion normal, porque el cliente pago de verdad y el saldo no
llego a ningun telefono.

Por eso hay dos banderas y no una. En produccion las dos son falsas y el
comprobante no menciona ningun ambiente: un ticket productivo que dijera
"sandbox" seria un ticket que nadie se cree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["Comprobante", "construir", "ETIQUETAS_ESTADO"]

#: Estado de la orden traducido a algo que una persona entiende.
#:
#: PAID no dice "completada" a proposito: el dinero esta cobrado y el servicio
#: puede seguir en curso. Un comprobante que dijera COMPLETADA ahi estaria
#: afirmando algo que todavia no ocurrio.
ETIQUETAS_ESTADO: dict[str, str] = {
    "CREATED": "PENDIENTE DE PAGO",
    "PAYMENT_PENDING": "ESPERANDO PAGO",
    "PAID": "PAGADA - EN PROCESO",
    "PROCESSING": "EN PROCESO",
    "SUCCESS": "COMPLETADA",
    "FAILED": "FALLIDA",
    "UNDER_REVIEW": "EN REVISION",
    "REFUND_PENDING": "REEMBOLSO PENDIENTE",
    "REFUNDED": "REEMBOLSADA",
    "EXPIRED": "EXPIRADA",
    "CANCELLED": "CANCELADA",
}

#: Como se le dice al cliente cada metodo de pago.
ETIQUETAS_PAGO: dict[str, str] = {
    "CASH": "Efectivo",
    "CARD": "Tarjeta",
}


@dataclass(frozen=True, slots=True)
class Comprobante:
    """Todo lo que el comprobante puede mostrar. Nada mas.

    Es inmutable y son todos ``str`` o ``bool`` ya formateados. La plantilla
    no calcula nada: si un importe sale mal, sale mal aqui, donde hay pruebas.
    """

    # -- identidad ---------------------------------------------------------
    folio: str
    fecha: datetime | None
    tienda: str
    tienda_direccion: str
    tienda_ciudad: str
    cajero: str

    # -- operacion ---------------------------------------------------------
    operador: str
    producto: str
    numero_enmascarado: str

    # -- dinero ------------------------------------------------------------
    valor_nominal: str
    comision: str
    total_pagado: str
    cambio: str
    metodo_pago: str

    # -- resultado ---------------------------------------------------------
    estado: str
    folio_proveedor: str
    etiqueta_folio_proveedor: str

    # -- ambiente ----------------------------------------------------------
    #: El COBRO se hizo contra un sandbox.
    cobro_en_pruebas: bool
    #: El SERVICIO se ejecuto contra un sandbox. Implica que el saldo no llego
    #: a ningun telefono real, y eso hay que decirlo con esas palabras.
    servicio_en_pruebas: bool

    @property
    def hay_aviso_de_pruebas(self) -> bool:
        return self.cobro_en_pruebas or self.servicio_en_pruebas

    @property
    def es_productivo(self) -> bool:
        """En produccion el comprobante no menciona ningun ambiente."""
        return not self.hay_aviso_de_pruebas


def _texto(valor: Any) -> str:
    """A cadena, sin ``None`` ni ``"None"`` colandose al ticket."""
    return "" if valor is None else str(valor).strip()


def _nombre_del_cajero(orden: dict, buscar_usuario: Any) -> str:
    """Quien HIZO la venta, no quien esta mirando el comprobante.

    El comprobante mostraba ``request.user``, asi que el dueno abriendo el
    ticket de un cajero veia su propio nombre impreso como cajero. En un
    negocio con varios turnos eso hace la auditoria imposible: el ticket
    miente sobre quien cobro.

    El nombre se busca por ``created_by_id``, que es lo que la orden guarda.
    Si no se encuentra el usuario -dado de baja, por ejemplo- se deja vacio
    antes que poner un nombre equivocado.
    """
    creador = orden.get("created_by_id")
    if not creador:
        return ""
    try:
        usuario = buscar_usuario(creador)
    except Exception:  # noqa: BLE001 - el ticket no se cae por esto
        return ""
    if usuario is None:
        return ""
    nombre = _texto(getattr(usuario, "get_short_name", lambda: "")())
    return nombre or _texto(getattr(usuario, "email", ""))


def construir(
    *,
    orden: dict,
    cumplimiento: dict | None,
    tienda: Any,
    buscar_usuario: Any,
    cambio_display: str = "",
) -> Comprobante:
    """Arma el comprobante a partir de lo que devolvieron los servicios.

    ``buscar_usuario`` se recibe como parametro en vez de importar el modelo
    aqui: asi este modulo se puede probar sin base de datos, que es lo que
    permite que las pruebas de "nunca muestres un token" sean rapidas y no
    dependan de fixtures.
    """
    cumplimiento = cumplimiento or {}

    # Operador y producto por separado. La descripcion de la orden los trae
    # pegados ("Telcel Recarga $100"), y el requisito pide el nombre EXACTO
    # del producto: si el cliente reclama, lo que compara es ese nombre.
    operador = _texto(cumplimiento.get("operator_name"))
    producto = _texto(cumplimiento.get("product_label"))
    if not operador and not producto:
        # Pago de servicios y casos sin cumplimiento: la descripcion es lo
        # unico que hay, y es mejor que una linea vacia.
        producto = _texto(orden.get("description"))

    # Folio del proveedor: primero el del operador, que es el que vale para
    # reclamar ante Telcel. Muchos operadores no lo devuelven; entonces sirve
    # el del proveedor, que existe siempre que la recarga se envio. Sin
    # ninguno de los dos el cliente no tiene con que reclamar nada.
    folio_operador = _texto(cumplimiento.get("operator_reference"))
    folio_proveedor = _texto(cumplimiento.get("provider_reference"))
    folio_servicio = _texto(cumplimiento.get("biller_reference"))
    if folio_operador:
        folio, etiqueta = folio_operador, "Folio operador"
    elif folio_servicio:
        folio, etiqueta = folio_servicio, "Folio del servicio"
    elif folio_proveedor:
        folio, etiqueta = folio_proveedor, "Folio proveedor"
    else:
        folio, etiqueta = "", ""

    estado_crudo = _texto(orden.get("state"))
    metodo_crudo = _texto(orden.get("payment_method"))

    return Comprobante(
        folio=_texto(orden.get("folio")),
        fecha=orden.get("created_at"),
        tienda=_texto(getattr(tienda, "name", "")),
        tienda_direccion=_texto(getattr(tienda, "address", "")),
        tienda_ciudad=", ".join(
            p
            for p in (
                _texto(getattr(tienda, "city", "")),
                _texto(getattr(tienda, "state", "")),
            )
            if p
        ),
        cajero=_nombre_del_cajero(orden, buscar_usuario),
        operador=operador,
        producto=producto,
        # Ya viene enmascarado del microservicio. Aqui no se desenmascara
        # nada: el numero completo no cruza esta frontera.
        numero_enmascarado=_texto(cumplimiento.get("phone_masked"))
        or _texto(cumplimiento.get("reference_masked")),
        valor_nominal=_texto(orden.get("base_display")),
        comision=_texto(orden.get("commission_display")),
        total_pagado=_texto(orden.get("total_display")),
        cambio=_texto(cambio_display),
        metodo_pago=ETIQUETAS_PAGO.get(metodo_crudo, metodo_crudo),
        estado=ETIQUETAS_ESTADO.get(estado_crudo, estado_crudo),
        folio_proveedor=folio,
        etiqueta_folio_proveedor=etiqueta,
        # Fail-closed: lo que no diga PRODUCTION explicitamente se trata como
        # pruebas. Un ambiente vacio o desconocido con el aviso puesto es un
        # susto; sin el aviso es un cliente que cree que recibio su saldo.
        cobro_en_pruebas=_texto(orden.get("provider_mode")).upper() != "PRODUCTION",
        servicio_en_pruebas=bool(cumplimiento)
        and _texto(cumplimiento.get("provider_mode")).upper() != "PRODUCTION",
    )
