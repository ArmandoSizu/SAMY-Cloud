"""Tarea de sincronizacion del catalogo."""

from __future__ import annotations

import structlog
from celery import shared_task

from apps.catalog.sync import sync_catalog
from samy_common.providers.exceptions import ProviderError

log = structlog.get_logger("catalog.tasks")


@shared_task(name="apps.catalog.tasks.sync_catalog_task")
def sync_catalog_task(provider_slug: str | None = None) -> dict[str, object]:
    """Refresca el catalogo desde el proveedor.

    Un fallo NO se propaga como excepcion de Celery: mientras no haya
    credenciales, esta tarea fallara cada seis horas, y llenar el registro de
    errores con algo esperado esconde los errores que si importan. Se registra
    como aviso y se devuelve el motivo.
    """
    try:
        run = sync_catalog(provider_slug)
    except ProviderError as exc:
        log.warning(
            "catalog_sync_skipped",
            provider=exc.provider,
            code=exc.code,
            reason=exc.message,
        )
        return {"succeeded": False, "reason": exc.message, "code": exc.code}

    return {
        "succeeded": True,
        "operators": run.operators_found,
        "products": run.products_found,
        "created": run.products_created,
        "updated": run.products_updated,
        "deactivated": run.products_deactivated,
    }
