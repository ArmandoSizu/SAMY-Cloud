"""Sandbox y produccion no se mezclan, y los dos sentidos importan.

Hay dos fallos simetricos que estas pruebas fijan:

  * produccion vendiendo contra un sandbox -> se cobra y no se entrega;
  * pruebas pegadas a produccion -> se gasta saldo real en silencio.

La tentacion es proteger solo el primero. El segundo es el que arruina una
tarde de desarrollo con recargas reales a telefonos reales.
"""

from __future__ import annotations

import pytest

from samy_common.providers.base import ProviderMode
from samy_common.providers.environment import (
    ProviderEnvironmentMismatch,
    RuntimeEnvironment,
    modo_esperado,
    resolver_ambiente,
    verificar_ambiente,
)


class TestResolverAmbiente:
    """Fail-closed: lo que no se reconoce no se interpreta."""

    @pytest.mark.parametrize(
        ("crudo", "esperado"),
        [
            ("production", RuntimeEnvironment.PRODUCTION),
            ("PROD", RuntimeEnvironment.PRODUCTION),
            ("  Production  ", RuntimeEnvironment.PRODUCTION),
            ("development", RuntimeEnvironment.DEVELOPMENT),
            ("local", RuntimeEnvironment.DEVELOPMENT),
            ("test", RuntimeEnvironment.TEST),
            ("ci", RuntimeEnvironment.TEST),
            ("staging", RuntimeEnvironment.STAGING),
        ],
    )
    def test_valores_reconocidos(self, crudo: str, esperado: RuntimeEnvironment) -> None:
        assert resolver_ambiente(crudo) is esperado

    def test_vacio_se_rechaza_no_se_asume_desarrollo(self) -> None:
        with pytest.raises(ProviderEnvironmentMismatch) as exc:
            resolver_ambiente("")
        assert "ENVIRONMENT no esta definido" in exc.value.message

    @pytest.mark.parametrize("crudo", ["produccion", "live", "prd", "real", "qa"])
    def test_desconocido_se_rechaza(self, crudo: str) -> None:
        """"produccion" en espanol NO cuenta. Aceptar variantes es el riesgo."""
        with pytest.raises(ProviderEnvironmentMismatch):
            resolver_ambiente(crudo)


class TestModoEsperado:
    def test_solo_produccion_espera_modo_produccion(self) -> None:
        assert modo_esperado(RuntimeEnvironment.PRODUCTION) is ProviderMode.PRODUCTION

    @pytest.mark.parametrize(
        "ambiente",
        [
            RuntimeEnvironment.DEVELOPMENT,
            RuntimeEnvironment.TEST,
            RuntimeEnvironment.STAGING,
        ],
    )
    def test_todo_lo_demas_espera_sandbox(self, ambiente: RuntimeEnvironment) -> None:
        """Staging incluido: un staging con credenciales reales cobra de verdad."""
        assert modo_esperado(ambiente) is ProviderMode.SANDBOX


class TestVerificarAmbiente:
    def test_produccion_con_produccion_pasa(self) -> None:
        verificar_ambiente(
            provider_slug="taecel",
            provider_mode=ProviderMode.PRODUCTION,
            ambiente=RuntimeEnvironment.PRODUCTION,
        )

    def test_desarrollo_con_sandbox_pasa(self) -> None:
        verificar_ambiente(
            provider_slug="reloadly",
            provider_mode=ProviderMode.SANDBOX,
            ambiente=RuntimeEnvironment.DEVELOPMENT,
        )

    def test_produccion_con_sandbox_se_detiene(self) -> None:
        """Se cobraria dinero real y se entregaria contra un sandbox."""
        with pytest.raises(ProviderEnvironmentMismatch) as exc:
            verificar_ambiente(
                provider_slug="reloadly",
                provider_mode=ProviderMode.SANDBOX,
                ambiente=RuntimeEnvironment.PRODUCTION,
            )
        assert "sin servicio" in exc.value.message
        assert "reloadly" in exc.value.message

    def test_pruebas_con_produccion_se_detiene(self) -> None:
        """La suite no puede gastar saldo real. Este es el caso de TAECEL."""
        with pytest.raises(ProviderEnvironmentMismatch) as exc:
            verificar_ambiente(
                provider_slug="taecel",
                provider_mode=ProviderMode.PRODUCTION,
                ambiente=RuntimeEnvironment.TEST,
            )
        assert "telefonos reales" in exc.value.message

    def test_staging_con_produccion_se_detiene(self) -> None:
        with pytest.raises(ProviderEnvironmentMismatch):
            verificar_ambiente(
                provider_slug="conekta",
                provider_mode=ProviderMode.PRODUCTION,
                ambiente=RuntimeEnvironment.STAGING,
            )

    def test_los_dos_mensajes_son_distintos(self) -> None:
        """Son dos errores distintos y hay que poder distinguirlos leyendo."""
        with pytest.raises(ProviderEnvironmentMismatch) as prod_con_sandbox:
            verificar_ambiente(
                provider_slug="x",
                provider_mode=ProviderMode.SANDBOX,
                ambiente=RuntimeEnvironment.PRODUCTION,
            )
        with pytest.raises(ProviderEnvironmentMismatch) as test_con_prod:
            verificar_ambiente(
                provider_slug="x",
                provider_mode=ProviderMode.PRODUCTION,
                ambiente=RuntimeEnvironment.TEST,
            )
        assert prod_con_sandbox.value.message != test_con_prod.value.message
