"""Conciliacion de recargas de Linntae: averiguar que paso, sin reintentar.

CUANDO SE USA
-------------

Cuando ``POST /purchase/tae`` no dio una respuesta concluyente: timeout, 500,
503, ``code 22`` o ``code 24``. En todos esos casos la recarga **puede** estar
aplicada, y la unica accion legitima es preguntar.

LAS DOS PREGUNTAS QUE LINNTAE ACEPTA, Y LO QUE NO ACEPTA
--------------------------------------------------------

``POST /sale/checkTransacctionTae``
    Identifica la transaccion por ``idOffer`` + ``phoneNumber``. Su
    documentacion dice que sirve durante los **primeros 60 segundos**
    posteriores a la venta. Pasada esa ventana su respuesta no significa lo
    mismo, asi que aqui simplemente no se llama.

``POST /sale/list``
    Historico, filtrable por fecha y por ``reference`` (que en tiempo aire es
    el telefono). Es la fuente para todo lo que ya salio de los 60 segundos.

Y lo que **no** acepta, que es lo importante: ninguno de los dos recibe la
``authorization`` de la compra ni una clave de idempotencia nuestra. Linntae
no ofrece "dime el estado de la operacion X". Por eso la conciliacion
necesita el CONTEXTO de la recarga -oferta, telefono, monto, momento de
envio- y no basta con el folio, que es lo que el contrato generico de
``TopupProvider`` sabe pasar.

EL FOLIO DE LINNTAE NO ES UNICO
-------------------------------

En el propio ejemplo de su especificacion, dos ventas distintas -$200 a un
telefono y $10 a otro, con doce minutos de diferencia- comparten el folio
``"12311057912"``. Emparejar por folio uniria operaciones que no son la misma
y cerraria una recarga con la evidencia de otra. El identificador unico de una
venta es ``id``.

LA REGLA DE ORO
---------------

    Se cierra como exitosa o como fallida SOLO con un registro que lo diga.

La AUSENCIA de registro **no** cierra nada. Podria significar que la venta no
existio, y podria significar que el historico todavia no la refleja; la
especificacion no dice en cuanto tiempo se indexa una venta. Entre las dos
lecturas no se elige: queda en revision. Cuesta que una persona lo mire; la
alternativa cuesta reembolsar a clientes que si recibieron su saldo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import structlog
from django.utils import timezone

from apps.providers.base import TopupResult, TopupStatus
from apps.providers.linntae.client import LinntaeClient, RespuestaLinntae
from apps.providers.linntae.codigos import Consecuencia, Endpoint
from apps.providers.linntae.parseo import RespuestaIlegible, VentaLinntae, leer_ventas
from samy_common.money import Money
from samy_common.providers.exceptions import ProviderError

__all__ = [
    "ContextoRecarga",
    "VENTANA_CONSULTA_INMEDIATA_SEGUNDOS",
    "VENTANA_EMPAREJAMIENTO_MINUTOS",
    "consultar_inmediata",
    "buscar_en_historico",
    "resolver",
]

log = structlog.get_logger("provider.linntae.conciliacion")

#: Linntae documenta 60 segundos para ``checkTransacctionTae``. Se usa un
#: margen corto por encima para absorber el retraso entre el envio y el
#: momento en que corre la conciliacion, sin llegar a preguntar fuera de la
#: ventana en la que su respuesta significa algo.
VENTANA_CONSULTA_INMEDIATA_SEGUNDOS: Final[int] = 75

#: Holgura al emparejar una venta del historico con nuestro envio. El reloj de
#: Linntae y el nuestro no estan sincronizados y su ``registerDate`` no lleva
#: zona horaria.
VENTANA_EMPAREJAMIENTO_MINUTOS: Final[int] = 15

#: Formato de ``registerDate``: "11/07/2025 13:09:47".
_FORMATOS_FECHA: Final[tuple[str, ...]] = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)

#: ``productType`` de tiempo aire en ``/sale/list``.
PRODUCT_TYPE_TAE: Final[int] = 1


@dataclass(frozen=True, slots=True)
class ContextoRecarga:
    """Todo lo que hace falta para preguntarle a Linntae por UNA recarga.

    No incluye la ``authorization`` como dato de busqueda porque Linntae no
    busca por ella. Se lleva solo para poder devolverla en el resultado.
    """

    id_offer: str
    telefono_nacional: str
    monto: Money
    #: Momento en que se envio la compra. Decide que pregunta se puede hacer.
    enviado_en: datetime | None = None
    autorizacion: str = ""

    @property
    def edad_segundos(self) -> float | None:
        if self.enviado_en is None:
            return None
        return max(0.0, (timezone.now() - self.enviado_en).total_seconds())

    @property
    def admite_consulta_inmediata(self) -> bool:
        edad = self.edad_segundos
        return edad is not None and edad <= VENTANA_CONSULTA_INMEDIATA_SEGUNDOS


def _fecha_linntae(momento: datetime) -> str:
    """``dd/MM/yyyy`` en hora local, que es la que Linntae usa.

    Se convierte a hora local antes de formatear. Sin eso, una venta hecha a
    las 19:00 de Mexico se buscaria en el dia siguiente en UTC y no
    apareceria: la conciliacion concluiria "no hay registro" justo en las
    ventas de la tarde.
    """
    return timezone.localtime(momento).strftime("%d/%m/%Y")


def _parsear_fecha(crudo: str) -> datetime | None:
    for formato in _FORMATOS_FECHA:
        try:
            return datetime.strptime(crudo, formato)
        except (ValueError, TypeError):
            continue
    return None


def consultar_inmediata(
    client: LinntaeClient, contexto: ContextoRecarga
) -> RespuestaLinntae | None:
    """``checkTransacctionTae``, solo dentro de su ventana de 60 segundos.

    Devuelve ``None`` cuando la recarga ya salio de esa ventana: preguntar
    fuera de ella daria una respuesta cuyo significado la especificacion no
    define, y una respuesta que no se sabe interpretar no es informacion.
    """
    if not contexto.admite_consulta_inmediata:
        return None

    try:
        return client.leer(
            Endpoint.CONSULTA_TAE,
            metodo="POST",
            cuerpo={
                "idOffer": int(contexto.id_offer),
                "phoneNumber": contexto.telefono_nacional,
            },
        )
    except (ProviderError, ValueError) as exc:
        log.warning(
            "linntae_consulta_inmediata_fallo",
            error=str(exc)[:200],
            telefono=contexto.telefono_nacional[-4:],
        )
        return None


def buscar_en_historico(
    client: LinntaeClient, contexto: ContextoRecarga
) -> list[VentaLinntae]:
    """Ventas de Linntae que podrian ser esta recarga.

    Filtra por telefono y por el dia, y despues empareja en memoria por monto
    y por cercania temporal. El filtro por dia se manda a Linntae; el resto se
    hace aqui porque su API no permite filtrar por monto.

    Se consulta el dia del envio y tambien el anterior cuando el envio fue de
    madrugada: una venta hecha a las 00:02 puede estar registrada el dia
    anterior si su reloj va unos minutos atras.
    """
    momento = contexto.enviado_en or timezone.now()
    dias = {_fecha_linntae(momento)}
    if timezone.localtime(momento).hour == 0:
        dias.add(_fecha_linntae(momento - timedelta(days=1)))

    encontradas: list[VentaLinntae] = []
    for dia in sorted(dias):
        try:
            respuesta = client.leer(
                Endpoint.VENTAS,
                metodo="POST",
                cuerpo={
                    "dateStart": dia,
                    "dateEnd": dia,
                    "reference": contexto.telefono_nacional,
                    "productType": PRODUCT_TYPE_TAE,
                    "offset": 0,
                    "max": 50,
                },
            )
        except ProviderError as exc:
            log.warning("linntae_historico_fallo", dia=dia, error=str(exc)[:200])
            continue

        if not respuesta.ok:
            continue

        try:
            ventas = leer_ventas(respuesta.payload)
        except RespuestaIlegible as exc:
            log.warning("linntae_historico_ilegible", dia=dia, error=str(exc)[:200])
            continue

        encontradas.extend(ventas)

    return [v for v in encontradas if _coincide(v, contexto)]


def _coincide(venta: VentaLinntae, contexto: ContextoRecarga) -> bool:
    """Si esta venta de Linntae es, con toda probabilidad, nuestra recarga.

    Tres condiciones, y las tres son necesarias:

    1. La referencia es el mismo telefono. Se comparan solo digitos porque
       Linntae devuelve la referencia tal como se capturo.
    2. El monto coincide con el valor facial. Se acepta que el importe venga
       en ``amount`` o en ``totalAmount``: el primero es la venta sin
       comisiones y el segundo con ellas, y con ``extraComision=0`` deberian
       ser iguales, pero no se apuesta a cual reporta.
    3. Esta dentro de la ventana temporal del envio. Sin esto, una recarga
       legitima de ayer al mismo telefono por el mismo monto se emparejaria
       con la de hoy.

    Si falta la fecha de envio no se exige la tercera: se prefiere devolver un
    candidato que una persona pueda mirar a no devolver nada.
    """
    digitos_venta = "".join(c for c in venta.referencia if c.isdigit())
    digitos_contexto = "".join(c for c in contexto.telefono_nacional if c.isdigit())
    if not digitos_venta or digitos_venta[-10:] != digitos_contexto[-10:]:
        return False

    montos = {m.cents for m in (venta.monto, venta.total) if m is not None}
    if montos and contexto.monto.cents not in montos:
        return False

    if contexto.enviado_en is None:
        return True

    registrada = _parsear_fecha(venta.registrada_en)
    if registrada is None:
        # Sin fecha legible no se descarta: se deja pasar como candidato y que
        # decida el conteo de candidatos.
        return True

    referencia = timezone.localtime(contexto.enviado_en).replace(tzinfo=None)
    return abs((registrada - referencia).total_seconds()) <= (
        VENTANA_EMPAREJAMIENTO_MINUTOS * 60
    )


def resolver(
    client: LinntaeClient, contexto: ContextoRecarga, *, modo: str
) -> TopupResult | None:
    """Que paso con esta recarga. ``None`` significa **no lo se**.

    Orden de las preguntas, de la mas fiable a la menos:

    1. ``checkTransacctionTae``, si la recarga esta dentro de sus 60 segundos.
       Es la respuesta mas directa: un ``code 0`` con autorizacion cierra el
       caso.
    2. El historico de ventas. Un registro con ``successTransaction`` cierra
       el caso en el sentido que diga.

    Y lo que no hace: concluir por ausencia. Si ninguna pregunta encuentra la
    venta, devuelve ``None`` y la recarga se queda en revision. Ver la regla de
    oro en el docstring del modulo.
    """
    inmediata = consultar_inmediata(client, contexto)
    if inmediata is not None:
        if inmediata.consecuencia is Consecuencia.EJECUTADA:
            autorizacion = str(inmediata.payload.get("authorization") or "").strip()
            if autorizacion:
                log.info(
                    "linntae_conciliada_por_consulta_inmediata",
                    telefono=contexto.telefono_nacional[-4:],
                )
                return TopupResult(
                    status=TopupStatus.SUCCEEDED,
                    provider_reference=autorizacion,
                    provider_mode=modo,
                    operator_reference=autorizacion,
                    delivered_amount=contexto.monto,
                    raw_response=dict(inmediata.payload),
                )
        # Cualquier otra respuesta de esta consulta -incluido code 24- no
        # cierra el caso. Dice que hay algo, no que fuera exitoso. Se sigue al
        # historico, que es donde esta el campo que lo afirma.

    candidatas = buscar_en_historico(client, contexto)

    if len(candidatas) == 1:
        venta = candidatas[0]
        if venta.exitosa is True:
            return TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference=str(venta.id_venta or contexto.autorizacion or ""),
                provider_mode=modo,
                operator_reference=venta.folio,
                delivered_amount=venta.monto or contexto.monto,
                raw_response=dict(venta.raw),
            )
        if venta.exitosa is False:
            return TopupResult(
                status=TopupStatus.FAILED,
                provider_reference=str(venta.id_venta or ""),
                provider_mode=modo,
                operator_reference=venta.folio,
                failure_reason=(venta.mensaje or "Linntae la registro como fallida.")[:255],
                raw_response=dict(venta.raw),
            )
        # El registro existe pero no afirma si fue exitosa. No se interpreta.
        log.warning(
            "linntae_venta_sin_veredicto",
            id_venta=venta.id_venta,
            telefono=contexto.telefono_nacional[-4:],
        )
        return None

    if len(candidatas) > 1:
        # Dos ventas indistinguibles al mismo telefono por el mismo monto en
        # la misma ventana. Elegir una seria elegir al azar de que operacion
        # es la evidencia.
        log.error(
            "linntae_conciliacion_ambigua",
            candidatas=len(candidatas),
            telefono=contexto.telefono_nacional[-4:],
            ids=[v.id_venta for v in candidatas],
        )
        return None

    log.info(
        "linntae_sin_evidencia",
        telefono=contexto.telefono_nacional[-4:],
        id_offer=contexto.id_offer,
    )
    return None


def contexto_desde_datos(
    *,
    id_offer: Any,
    telefono_nacional: str,
    monto: Money,
    enviado_en: datetime | None = None,
    autorizacion: str = "",
) -> ContextoRecarga:
    """Constructor tolerante, para quien tiene los datos en texto."""
    return ContextoRecarga(
        id_offer=str(id_offer).strip(),
        telefono_nacional="".join(c for c in str(telefono_nacional) if c.isdigit()),
        monto=monto,
        enviado_en=enviado_en,
        autorizacion=str(autorizacion or "").strip(),
    )
