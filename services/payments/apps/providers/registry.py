"""Registro y construccion de proveedores de pago.

El registro desacopla "que proveedores existen" de "cual usa esta tienda".
Agregar Stripe u Openpay manana es escribir un adaptador y decorarlo con
``@payment_registry.register``: ni la logica de ordenes ni la UI cambian.

``get_provider()`` es la unica puerta por la que el servicio obtiene un
adaptador, y siempre construye a partir de configuracion de entorno. No hay
forma de instanciar un proveedor con credenciales codificadas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from django.conf import settings

from samy_common.providers.base import ProviderMode, ProviderRegistry

if TYPE_CHECKING:  # pragma: no cover
    from apps.providers.base import PaymentProvider

log = structlog.get_logger("providers")

#: Registro global del servicio de pagos.
payment_registry = ProviderRegistry()


def _mode(raw: str) -> ProviderMode:
    """Traduce la configuracion a modo, con SANDBOX como valor seguro.

    Ante un valor invalido se elige SANDBOX, nunca PRODUCTION: un error de
    configuracion debe degradar hacia lo inofensivo, no hacia cobrar dinero
    real por accidente.
    """
    value = (raw or "").strip().upper()
    if value == "PRODUCTION":
        return ProviderMode.PRODUCTION
    if value and value != "SANDBOX":
        log.warning("provider_mode_invalid_defaulting_to_sandbox", value=raw)
    return ProviderMode.SANDBOX


def get_provider(slug: str) -> "PaymentProvider":
    """Construye el adaptador solicitado con la configuracion del entorno."""
    # Importacion diferida: los modulos de adaptadores se registran a si
    # mismos al importarse, y hacerlo aqui evita un ciclo de importacion.
    from apps.providers import cash, conekta  # noqa: F401

    provider_cls = payment_registry.get(slug)

    if slug == "cash":
        return provider_cls(  # type: ignore[return-value]
            cash.CashConfig(enabled=settings.CASH_PAYMENT_ENABLED),
            ProviderMode.PRODUCTION,
        )

    if slug == "conekta":
        return provider_cls(  # type: ignore[return-value]
            conekta.ConektaConfig(
                private_key=settings.CONEKTA_PRIVATE_KEY,
                public_key=settings.CONEKTA_PUBLIC_KEY,
                webhook_public_key=settings.CONEKTA_WEBHOOK_PUBLIC_KEY,
            ),
            _mode(settings.CONEKTA_MODE),
        )

    raise NotImplementedError(  # pragma: no cover
        f"El proveedor '{slug}' esta registrado pero no tiene constructor. "
        "Agregalo en apps/providers/registry.py."
    )


def get_provider_for_method(method: str) -> "PaymentProvider":
    """Elige el proveedor segun el metodo de pago solicitado.

    Mapeo explicito y no automatico: que el efectivo lo maneje el adaptador de
    efectivo y la tarjeta la pasarela debe ser una decision visible en el
    codigo, no un efecto de la primera capacidad que coincida.
    """
    mapping = {
        "CASH": "cash",
        "CARD": settings.CARD_PAYMENT_PROVIDER,
        "TRANSFER": settings.TRANSFER_PAYMENT_PROVIDER,
        "QR": settings.QR_PAYMENT_PROVIDER,
    }
    slug = mapping.get(method.upper())
    if not slug:
        from samy_common.providers.exceptions import ProviderNotFound

        raise ProviderNotFound(
            provider=method,
            message=f"No hay proveedor configurado para el metodo '{method}'.",
        )
    return get_provider(slug)


def describe_all() -> list[dict[str, object]]:
    """Metadatos y salud de todos los adaptadores, para el panel de plataforma."""
    from apps.providers import cash, conekta  # noqa: F401

    described = []
    for meta in payment_registry.describe_all():
        entry = dict(meta)
        try:
            provider = get_provider(str(meta["slug"]))
            health = provider.check_health()
            entry["status"] = str(health.status)
            entry["detail"] = health.detail
            entry["missing_requirements"] = list(health.missing_requirements)
            entry["latency_ms"] = health.latency_ms
            entry["mode"] = str(provider.mode)

            # La llave PUBLICA de la pasarela se expone a proposito: el
            # navegador la necesita para inicializar el tokenizador. Es
            # publica por diseno y solo sirve para tokenizar, no para cobrar.
            #
            # La llave PRIVADA no sale de aqui jamas, ni siquiera truncada.
            entry["public_key"] = str(
                getattr(provider.config, "public_key", "") or ""
            )
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "ERROR"
            entry["detail"] = str(exc)[:300]
            entry["missing_requirements"] = []
            entry["mode"] = ""
            entry["public_key"] = ""
        described.append(entry)
    return described
