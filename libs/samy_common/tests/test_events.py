"""Pruebas del bus de eventos.

El caso central es el que rompio la entrega de recargas en un entorno real:
``XREADGROUP`` devuelve las claves del hash en **bytes**, no en texto. Al
buscarlas como texto no se encontraba ninguna, el evento se reconstruia con
todos los campos vacios y ``datetime.fromisoformat("")`` reventaba. El
consumidor registraba "event_unparseable", confirmaba el mensaje para no
atascar la cola y seguia adelante: una orden pagada cuya recarga no se
ejecutaba nunca, sin ningun error visible para el cajero.

Por eso estas pruebas comprueban la forma exacta que entrega Redis, no una
version idealizada del diccionario.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone as dt_timezone

import pytest

from samy_common.events.bus import Event


def _evento() -> Event:
    return Event(
        event_id=str(uuid.uuid4()),
        event_type="order.paid",
        aggregate_type="Order",
        aggregate_id=str(uuid.uuid4()),
        correlation_id="abc123",
        occurred_at=datetime(2026, 9, 6, 2, 27, 4, 310310, tzinfo=dt_timezone.utc),
        payload={
            "order_id": "e7620690-24d5-4b07-97f4-f1cf7785ccc1",
            "service_kind": "TOPUP",
            "fulfillment_id": "0b5c5c62-fa8c-4c66-94a0-fb1b6ec73737",
            "total_cents": 8985,
        },
    )


def _como_lo_entrega_redis(wire: dict[str, str]) -> dict[bytes, bytes]:
    """Reproduce el formato de ``XREADGROUP`` sin ``decode_responses``."""
    return {k.encode(): v.encode() for k, v in wire.items()}


def test_ida_y_vuelta_con_claves_de_texto():
    original = _evento()
    recuperado = Event.from_wire(original.to_wire())

    assert recuperado.event_type == original.event_type
    assert recuperado.aggregate_id == original.aggregate_id
    assert recuperado.occurred_at == original.occurred_at
    assert recuperado.payload == original.payload


def test_ida_y_vuelta_con_claves_en_bytes():
    """Este es el formato REAL que entrega Redis Streams."""
    original = _evento()
    recuperado = Event.from_wire(_como_lo_entrega_redis(original.to_wire()))

    assert recuperado.event_id == original.event_id
    assert recuperado.event_type == "order.paid"
    assert recuperado.correlation_id == "abc123"
    assert recuperado.occurred_at == original.occurred_at
    # Lo que de verdad importaba: el payload llega entero.
    assert recuperado.payload["service_kind"] == "TOPUP"
    assert recuperado.payload["fulfillment_id"] == original.payload["fulfillment_id"]
    assert recuperado.payload["total_cents"] == 8985


def test_un_evento_en_bytes_no_pierde_el_fulfillment_id():
    """Regresion concreta: sin fulfillment_id la recarga nunca se encola."""
    original = _evento()
    recuperado = Event.from_wire(_como_lo_entrega_redis(original.to_wire()))

    assert recuperado.payload.get("fulfillment_id"), (
        "El consumidor descarta el evento si el payload llega vacio, y la "
        "orden queda pagada sin ejecutarse."
    )


def test_payload_ausente_no_revienta():
    wire = _evento().to_wire()
    del wire["payload"]

    recuperado = Event.from_wire(_como_lo_entrega_redis(wire))

    assert recuperado.payload == {}


def test_fecha_vacia_si_falla_lo_hace_ruidosamente():
    """Un evento sin fecha es un error real: no debe pasar silenciosamente."""
    wire = _evento().to_wire()
    wire["occurred_at"] = ""

    with pytest.raises(ValueError):
        Event.from_wire(_como_lo_entrega_redis(wire))


def test_to_wire_produce_solo_texto_plano():
    """Redis Streams no acepta estructuras anidadas: todo debe ser texto."""
    wire = _evento().to_wire()

    assert all(isinstance(v, str) for v in wire.values())
    # El payload viaja como JSON compacto, no como dict.
    assert json.loads(wire["payload"])["service_kind"] == "TOPUP"
