"""Registro de agregadores de pago de servicios."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from django.conf import settings

from samy_common.providers.base import ProviderMode, ProviderRegistry

if TYPE_CHECKING:  # pragma: no cover
    from apps.providers.base import BillerProvider

log = structlog.get_logger("providers")

biller_registry = ProviderRegistry()


def _mode(raw: str) -> ProviderMode:
    value = (raw or "").strip().upper()
    if value == "PRODUCTION":
        return ProviderMode.PRODUCTION
    if value and value != "SANDBOX":
        log.warning("provider_mode_invalid_defaulting_to_sandbox", value=raw)
    return ProviderMode.SANDBOX


def get_provider(slug: str | None = None) -> "BillerProvider":
    from apps.providers import tapi  # noqa: F401

    slug = slug or settings.BILLPAY_PROVIDER
    if slug in ("", "none"):
        # Sin agregador configurado. Se devuelve el adaptador de tapi, que
        # reportara PENDING_CONTRACT y explicara exactamente que falta.
        slug = "tapi"

    provider_cls = biller_registry.get(slug)

    if slug == "tapi":
        return provider_cls(  # type: ignore[return-value]
            tapi.TapiConfig(
                api_key=settings.TAPI_API_KEY, api_secret=settings.TAPI_API_SECRET
            ),
            _mode(settings.TAPI_MODE),
        )

    raise NotImplementedError(  # pragma: no cover
        f"El proveedor '{slug}' esta registrado pero no tiene constructor."
    )


def describe_all() -> list[dict[str, object]]:
    from apps.providers import tapi  # noqa: F401

    described = []
    for meta in biller_registry.describe_all():
        entry = dict(meta)
        try:
            health = get_provider(str(meta["slug"])).check_health()
            entry["status"] = str(health.status)
            entry["detail"] = health.detail
            entry["missing_requirements"] = list(health.missing_requirements)
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "ERROR"
            entry["detail"] = str(exc)[:300]
            entry["missing_requirements"] = []
        described.append(entry)
    return described
