"""Pruebas de la firma HMAC entre servicios."""

from __future__ import annotations

import time

import pytest

from samy_common.security.signing import (
    HEADER_TIMESTAMP,
    SignatureError,
    build_signature,
    sign_request,
    verify_request,
)

SECRETO = "un-secreto-de-prueba-suficientemente-largo"


def _firmar(body: bytes = b"", path: str = "/api/v1/orders/", method: str = "POST"):
    return sign_request(secret=SECRETO, method=method, path=path, service="core", body=body)


class TestFirmaValida:
    def test_se_acepta_y_devuelve_el_servicio(self):
        headers = _firmar(b'{"a":1}')
        servicio = verify_request(
            secret=SECRETO,
            method="POST",
            path="/api/v1/orders/",
            headers=headers.as_dict(),
            body=b'{"a":1}',
        )
        assert servicio == "core"

    def test_cuerpo_vacio(self):
        headers = _firmar(b"", method="GET")
        assert verify_request(
            secret=SECRETO, method="GET", path="/api/v1/orders/",
            headers=headers.as_dict(), body=b"",
        ) == "core"

    def test_dos_firmas_del_mismo_contenido_difieren(self):
        """El nonce hace que dos firmas identicas en contenido sean distintas,
        lo que impide reconocer peticiones repetidas por su firma."""
        a = _firmar(b'{"a":1}')
        b = _firmar(b'{"a":1}')
        assert a.signature != b.signature
        assert a.nonce != b.nonce


class TestRechazos:
    def test_cuerpo_alterado(self):
        headers = _firmar(b'{"amount":100}')
        with pytest.raises(SignatureError, match="invalida"):
            verify_request(
                secret=SECRETO, method="POST", path="/api/v1/orders/",
                headers=headers.as_dict(), body=b'{"amount":999999}',
            )

    def test_ruta_alterada(self):
        headers = _firmar(b"", path="/api/v1/orders/")
        with pytest.raises(SignatureError):
            verify_request(
                secret=SECRETO, method="POST", path="/api/v1/refunds/",
                headers=headers.as_dict(), body=b"",
            )

    def test_metodo_alterado(self):
        headers = _firmar(b"", method="GET")
        with pytest.raises(SignatureError):
            verify_request(
                secret=SECRETO, method="DELETE", path="/api/v1/orders/",
                headers=headers.as_dict(), body=b"",
            )

    def test_secreto_distinto(self):
        headers = _firmar(b"")
        with pytest.raises(SignatureError):
            verify_request(
                secret="otro-secreto", method="POST", path="/api/v1/orders/",
                headers=headers.as_dict(), body=b"",
            )

    def test_cabeceras_ausentes(self):
        with pytest.raises(SignatureError, match="Faltan cabeceras"):
            verify_request(
                secret=SECRETO, method="POST", path="/api/v1/orders/",
                headers={}, body=b"",
            )

    def test_timestamp_viejo(self):
        headers = _firmar(b"").as_dict()
        headers[HEADER_TIMESTAMP] = str(int(time.time()) - 3600)
        with pytest.raises(SignatureError, match="ventana"):
            verify_request(
                secret=SECRETO, method="POST", path="/api/v1/orders/",
                headers=headers, body=b"", tolerance_seconds=300,
            )

    def test_nonce_repetido(self):
        """Defensa contra repeticion: una peticion capturada no puede
        reenviarse aunque siga dentro de la ventana temporal."""
        headers = _firmar(b"")
        with pytest.raises(SignatureError, match="repeticion"):
            verify_request(
                secret=SECRETO, method="POST", path="/api/v1/orders/",
                headers=headers.as_dict(), body=b"", seen_nonce=True,
            )

    def test_secreto_vacio_falla_ruidosamente(self):
        with pytest.raises(SignatureError, match="vacio"):
            build_signature(
                secret="", method="POST", path="/x", timestamp="1",
                nonce="n", service="core",
            )
