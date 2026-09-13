"""Claves de idempotencia para las operaciones que inicia el cajero.

EL PROBLEMA
-----------

Los microservicios ya deduplican por ``Idempotency-Key``: hay un UNIQUE en
base de datos y un hash del cuerpo (ver ``samy_common.idempotency``). Esa
maquinaria es solida y no hace falta tocarla.

Lo que estaba mal era **la clave que enviaba el Core**:

    idem = uuid.uuid4().hex          # <- una clave nueva en CADA peticion

Con una clave aleatoria por peticion, la deduplicacion no puede funcionar: dos
clics seguidos producen dos claves distintas, y para el servicio de Pagos son
dos operaciones diferentes. La proteccion existia y no protegia nada, que es
peor que no tenerla, porque invita a confiar en ella.

LA REGLA
--------

    La clave identifica la OPERACION, no la peticion.

Si el cajero pulsa dos veces "Cobrar" sobre la misma orden, son dos peticiones
de UNA operacion. Deben llevar la misma clave y la segunda debe devolver el
resultado de la primera.

POR QUE EL IDENTIFICADOR SE ASIGNA AL CREAR EL BORRADOR
-------------------------------------------------------

La tentacion es generarlo en la vista que confirma, guardandolo en la sesion
si no existe. No sirve: un doble clic son dos peticiones casi simultaneas, las
dos cargan la sesion antes de que la primera la guarde, las dos ven que falta
el identificador y cada una genera el suyo. La carrera reaparece donde se
intentaba cerrar.

Asignandolo cuando se CREA el borrador -una peticion anterior y separada- las
dos peticiones del doble clic leen el mismo valor ya escrito.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from django.http import HttpRequest

__all__ = ["nuevo_id_operacion", "id_de_operacion", "clave"]

#: Nombre del campo dentro del borrador de sesion.
CAMPO: str = "op_id"


def nuevo_id_operacion() -> str:
    """Identificador de una operacion nueva. Se guarda en el borrador.

    Se llama al CREAR el borrador, no al confirmarlo. Ver la nota del modulo.
    """
    return uuid.uuid4().hex


def id_de_operacion(
    request: HttpRequest, clave_borrador: str, *, partes: tuple[Any, ...] = ()
) -> str:
    """Identificador estable de la operacion que describe el borrador.

    Lo normal es que el borrador ya traiga su ``op_id``. El respaldo por hash
    del contenido existe solo para sesiones creadas antes de que este campo
    existiera: sin el, esas sesiones volverian a la clave aleatoria y al
    problema original.

    El respaldo tiene una limitacion que conviene tener escrita: dos ventas
    identicas -mismo producto, mismo telefono, mismo importe- en la misma
    sesion vieja producen la misma clave, y la segunda devolveria el resultado
    de la primera en vez de crear una operacion nueva. Se acepta porque el
    error va en la direccion segura (una operacion de menos, nunca un cobro de
    mas) y porque afecta solo a sesiones que caducan en horas.
    """
    borrador = request.session.get(clave_borrador) or {}
    guardado = borrador.get(CAMPO)
    if guardado:
        return str(guardado)

    material = "|".join(str(p) for p in partes) or clave_borrador
    return "h" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def clave(prefijo: str, *partes: Any) -> str:
    """Arma una clave legible: ``pay-<orden>``, ``topup-<operacion>``.

    El prefijo importa: la misma operacion llama a varios endpoints (crear la
    recarga, crear la orden, enlazarlas) y cada llamada necesita su propia
    clave. Sin prefijo, la segunda llamada recibiria la respuesta guardada de
    la primera.
    """
    cuerpo = "-".join(str(p) for p in partes if p not in (None, ""))
    return f"{prefijo}-{cuerpo}" if cuerpo else prefijo
