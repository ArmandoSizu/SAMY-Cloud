"""Firma de peticiones servicio-a-servicio (S2S).

Los microservicios de SAMY Cloud no se confian entre si por estar en la misma
red. Cada peticion interna va firmada con HMAC-SHA256 sobre un string canonico
que incluye metodo, ruta, timestamp, nonce y hash del cuerpo.

Esto protege contra:

* **Suplantacion**: sin el secreto compartido no se puede forjar la firma.
* **Replay**: el timestamp tiene ventana de tolerancia y el nonce se consume
  una sola vez (cache Redis).
* **Manipulacion del cuerpo**: el hash del body entra en la firma.

Todas las comparaciones usan ``hmac.compare_digest`` para evitar filtrar
informacion por diferencias de tiempo.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Final, Mapping

__all__ = [
    "SignatureError",
    "SignedHeaders",
    "build_signature",
    "sign_request",
    "verify_request",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "HEADER_NONCE",
    "HEADER_SERVICE",
]

HEADER_SIGNATURE: Final[str] = "X-Samy-Signature"
HEADER_TIMESTAMP: Final[str] = "X-Samy-Timestamp"
HEADER_NONCE: Final[str] = "X-Samy-Nonce"
HEADER_SERVICE: Final[str] = "X-Samy-Service"

#: Version del esquema de firma, embebida en el string canonico. Permite
#: rotar el algoritmo sin romper servicios que aun no se han desplegado.
SIGNATURE_VERSION: Final[str] = "v1"

#: Ventana de tolerancia por defecto para el desfase de reloj, en segundos.
DEFAULT_TOLERANCE_SECONDS: Final[int] = 300


class SignatureError(Exception):
    """La firma de una peticion S2S no es valida."""


@dataclass(frozen=True, slots=True)
class SignedHeaders:
    """Cabeceras generadas al firmar una peticion."""

    signature: str
    timestamp: str
    nonce: str
    service: str

    def as_dict(self) -> dict[str, str]:
        return {
            HEADER_SIGNATURE: self.signature,
            HEADER_TIMESTAMP: self.timestamp,
            HEADER_NONCE: self.nonce,
            HEADER_SERVICE: self.service,
        }


def _canonical_string(
    *,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    service: str,
    body: bytes,
) -> str:
    """Construye el string canonico que se firma.

    El orden y los separadores son parte del contrato: cambiarlos invalida
    todas las firmas existentes, por eso va versionado.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join(
        [
            SIGNATURE_VERSION,
            method.upper(),
            path,
            timestamp,
            nonce,
            service,
            body_hash,
        ]
    )


def build_signature(
    *,
    secret: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    service: str,
    body: bytes = b"",
) -> str:
    """Calcula la firma HMAC-SHA256 en hexadecimal."""
    if not secret:
        raise SignatureError("El secreto S2S esta vacio; revisa la configuracion.")
    canonical = _canonical_string(
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        service=service,
        body=body,
    )
    return hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_request(
    *,
    secret: str,
    method: str,
    path: str,
    service: str,
    body: bytes = b"",
) -> SignedHeaders:
    """Firma una peticion saliente y devuelve las cabeceras a enviar."""
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    signature = build_signature(
        secret=secret,
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        service=service,
        body=body,
    )
    return SignedHeaders(
        signature=signature, timestamp=timestamp, nonce=nonce, service=service
    )


def verify_request(
    *,
    secret: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes = b"",
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    seen_nonce: bool = False,
) -> str:
    """Verifica una peticion S2S entrante. Devuelve el servicio emisor.

    ``seen_nonce`` lo aporta quien llama, consultando el cache de nonces
    consumidos. Se recibe como parametro en vez de consultarlo aqui para que
    este modulo no dependa de Redis y siga siendo trivial de probar.
    """
    signature = headers.get(HEADER_SIGNATURE) or headers.get(HEADER_SIGNATURE.lower())
    timestamp = headers.get(HEADER_TIMESTAMP) or headers.get(HEADER_TIMESTAMP.lower())
    nonce = headers.get(HEADER_NONCE) or headers.get(HEADER_NONCE.lower())
    service = headers.get(HEADER_SERVICE) or headers.get(HEADER_SERVICE.lower())

    if not all([signature, timestamp, nonce, service]):
        raise SignatureError("Faltan cabeceras de firma S2S.")

    assert signature and timestamp and nonce and service  # para el type checker

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("Timestamp de firma no numerico.") from exc

    drift = abs(int(time.time()) - ts)
    if drift > tolerance_seconds:
        raise SignatureError(
            f"Timestamp fuera de la ventana permitida ({drift}s > {tolerance_seconds}s)."
        )

    if seen_nonce:
        raise SignatureError("Nonce ya utilizado: posible ataque de repeticion.")

    expected = build_signature(
        secret=secret,
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        service=service,
        body=body,
    )
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("Firma S2S invalida.")

    return service
