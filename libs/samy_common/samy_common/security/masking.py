"""Enmascarado de datos sensibles para logs, comprobantes y APIs.

SAMY Cloud no almacena PAN completo ni CVV (ver ``docs/security.md``), pero si
maneja datos que identifican a un cliente: numeros telefonicos y referencias de
recibo. Esos datos aparecen en comprobantes que se imprimen y se quedan en el
mostrador, y en logs que consultan operadores.

Regla: en cualquier salida legible por humanos que no sea la pantalla de
confirmacion del propio cajero, la referencia va enmascarada.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "mask_phone",
    "mask_reference",
    "mask_pan",
    "mask_email",
    "scrub_text",
]

#: Patrones que jamas deben terminar en un log, aunque alguien los pase por error.
_PAN_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_CVV_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(cvv|cvc|cvv2|card_?number|pan|security_?code)\b\s*[:=]\s*\S+"
)


def mask_phone(phone: str) -> str:
    """``5512345678`` -> ``55****5678``.

    Deja los dos primeros digitos (lada) y los cuatro ultimos, suficiente para
    que el cajero verifique con el cliente sin exponer el numero completo.
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) <= 6:
        return "*" * len(digits)
    return f"{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"


def mask_reference(reference: str, *, keep_last: int = 4) -> str:
    """Enmascara una referencia de servicio dejando visibles los ultimos digitos."""
    value = (reference or "").strip()
    if len(value) <= keep_last:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep_last)}{value[-keep_last:]}"


def mask_pan(pan: str) -> str:
    """Enmascara un PAN dejando solo los ultimos 4 (formato permitido por PCI DSS)."""
    digits = re.sub(r"\D", "", pan or "")
    if len(digits) < 4:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def mask_email(email: str) -> str:
    """``sizu@example.com`` -> ``s***@example.com``."""
    if "@" not in (email or ""):
        return "*" * len(email or "")
    local, _, domain = email.partition("@")
    if not local:
        return f"*@{domain}"
    return f"{local[0]}{'*' * max(len(local) - 1, 1)}@{domain}"


def scrub_text(text: str) -> str:
    """Ultima linea de defensa: limpia PAN y CVV de un texto arbitrario.

    Se aplica en el procesador de logs para que ni un ``print`` accidental ni
    el cuerpo de un error de un proveedor filtren datos de tarjeta.
    """
    if not text:
        return text
    cleaned = _CVV_KEY_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    cleaned = _PAN_RE.sub(lambda m: mask_pan(m.group(0)), cleaned)
    return cleaned
