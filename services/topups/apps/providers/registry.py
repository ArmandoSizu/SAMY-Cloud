"""Registro de proveedores de recargas."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from django.conf import settings

from samy_common.providers.base import ProviderMode, ProviderRegistry

if TYPE_CHECKING:  # pragma: no cover
    from apps.providers.base import TopupProvider

log = structlog.get_logger("providers")

topup_registry = ProviderRegistry()


def _mode(raw: str) -> ProviderMode:
    """SANDBOX ante cualquier valor que no sea explicitamente PRODUCTION."""
    value = (raw or "").strip().upper()
    if value == "PRODUCTION":
        return ProviderMode.PRODUCTION
    if value and value != "SANDBOX":
        log.warning("provider_mode_invalid_defaulting_to_sandbox", value=raw)
    return ProviderMode.SANDBOX


def get_provider(slug: str | None = None) -> "TopupProvider":
    from apps.providers import reloadly, taecel  # noqa: F401

    slug = slug or settings.TOPUP_PROVIDER
    provider_cls = topup_registry.get(slug)

    if slug == "reloadly":
        return provider_cls(  # type: ignore[return-value]
            reloadly.ReloadlyConfig(
                client_id=settings.RELOADLY_CLIENT_ID,
                client_secret=settings.RELOADLY_CLIENT_SECRET,
            ),
            _mode(settings.RELOADLY_MODE),
        )

    if slug == "taecel":
        return provider_cls(  # type: ignore[return-value]
            taecel.TaecelConfig(key=settings.TAECEL_KEY, nip=settings.TAECEL_NIP),
            _mode(settings.TAECEL_MODE),
        )

    raise NotImplementedError(  # pragma: no cover
        f"El proveedor '{slug}' esta registrado pero no tiene constructor."
    )


def describe_all() -> list[dict[str, object]]:
    """Metadatos y salud de todos los adaptadores, para el panel de plataforma."""
    from apps.providers import reloadly, taecel  # noqa: F401

    described = []
    for meta in topup_registry.describe_all():
        entry = dict(meta)
        try:
            provider = get_provider(str(meta["slug"]))
            health = provider.check_health()
            # El modo es la respuesta a "estoy pegado al sandbox o a
            # produccion?". Sin el, el panel muestra un [OK] que no dice
            # contra que ambiente esta operando, que es justo lo que hay que
            # poder distinguir de un vistazo.
            entry["mode"] = str(provider.mode)
            entry["status"] = str(health.status)
            entry["detail"] = health.detail
            entry["missing_requirements"] = list(health.missing_requirements)
        except Exception as exc:  # noqa: BLE001
            entry["mode"] = ""
            entry["status"] = "ERROR"
            entry["detail"] = str(exc)[:300]
            entry["missing_requirements"] = []
        described.append(entry)
    return described
