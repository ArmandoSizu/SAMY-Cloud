"""Sincronizacion del catalogo desde el proveedor.

Estrategia: **upsert y desactivacion**, nunca borrado.

Un producto que desaparece del catalogo del proveedor se marca inactivo. No se
borra porque las ordenes historicas lo referencian: borrarlo dejaria
comprobantes de meses atras apuntando a un producto inexistente, y una
auditoria contable sin poder reconstruir que se vendio.

La sincronizacion completa corre dentro de una transaccion. Si falla a la
mitad, el catalogo anterior queda intacto: es preferible operar con un
catalogo de ayer que con medio catalogo de hoy.
"""

from __future__ import annotations

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.catalog.models import CatalogSyncRun, Operator, TopupProduct
from apps.providers.base import CatalogProduct
from apps.providers.registry import get_provider
from samy_common.providers.exceptions import ProviderError

log = structlog.get_logger("catalog.sync")

#: Operadores prioritarios y su orden en la pantalla del cajero. Esto NO es un
#: catalogo codificado: son solo preferencias de presentacion. Si el proveedor
#: no devuelve Telcel, Telcel no aparece por mucho que este en esta lista.
DISPLAY_PRIORITY: dict[str, int] = {
    "telcel": 10,
    "movistar": 20,
    "at-t": 30,
    "att": 30,
    "at-t-mexico": 30,
    "unefon": 40,
    "bait": 50,
    "weex": 60,
    "virgin-mobile": 70,
    "oui": 80,
}


def sync_catalog(provider_slug: str | None = None) -> CatalogSyncRun:
    """Descarga el catalogo del proveedor y lo refleja en la base de datos."""
    provider = get_provider(provider_slug)

    run = CatalogSyncRun.objects.create(
        provider_slug=provider.slug, provider_mode=str(provider.mode)
    )

    try:
        products = provider.fetch_catalog()
    except ProviderError as exc:
        run.finished_at = timezone.now()
        run.succeeded = False
        run.error_message = f"{exc.code}: {exc.message}"
        run.save()
        log.warning(
            "catalog_sync_failed",
            provider=provider.slug,
            code=exc.code,
            error=exc.message,
        )
        # Se propaga: quien invoca decide si es un fallo visible (sincronizacion
        # manual del administrador) o silencioso (tarea periodica).
        raise

    with transaction.atomic():
        seen_product_ids: set[str] = set()
        operators_seen: dict[str, Operator] = {}
        created = updated = 0

        for item in products:
            operator = _upsert_operator(item, provider.slug, operators_seen)
            _, was_created = _upsert_product(item, operator, provider.slug)
            created += int(was_created)
            updated += int(not was_created)
            seen_product_ids.add(item.provider_product_id)

        # Desactiva lo que el proveedor ya no ofrece.
        deactivated = (
            TopupProduct.objects.filter(provider_slug=provider.slug, is_active=True)
            .exclude(provider_product_id__in=seen_product_ids)
            .update(is_active=False, updated_at=timezone.now())
        )

        # Un operador sin ningun producto activo tampoco debe mostrarse.
        empty_operators = (
            Operator.objects.filter(provider_slug=provider.slug, is_active=True)
            .exclude(products__is_active=True)
            .update(is_active=False)
        )

        run.finished_at = timezone.now()
        run.succeeded = True
        run.operators_found = len(operators_seen)
        run.products_found = len(products)
        run.products_created = created
        run.products_updated = updated
        run.products_deactivated = deactivated
        run.save()

    log.info(
        "catalog_synced",
        provider=provider.slug,
        mode=str(provider.mode),
        operators=len(operators_seen),
        products=len(products),
        created=created,
        updated=updated,
        deactivated=deactivated,
        empty_operators_deactivated=empty_operators,
    )
    return run


def _upsert_operator(
    item: CatalogProduct, provider_slug: str, cache: dict[str, Operator]
) -> Operator:
    if item.operator_code in cache:
        return cache[item.operator_code]

    slug = slugify(item.operator_name)[:60]
    operator, _ = Operator.objects.update_or_create(
        provider_slug=provider_slug,
        provider_operator_id=item.operator_code,
        defaults={
            "name": item.operator_name,
            "slug": slug,
            "logo_url": item.logo_url,
            "supports_data_packages": item.is_data_package,
            "is_active": True,
            "display_order": DISPLAY_PRIORITY.get(slug, 100),
            "last_synced_at": timezone.now(),
        },
    )
    cache[item.operator_code] = operator
    return operator


def _upsert_product(
    item: CatalogProduct, operator: Operator, provider_slug: str
) -> tuple[TopupProduct, bool]:
    return TopupProduct.objects.update_or_create(
        provider_slug=provider_slug,
        provider_product_id=item.provider_product_id,
        defaults={
            "operator": operator,
            "label": item.label[:160],
            "currency": (item.amount or item.min_amount).currency
            if (item.amount or item.min_amount)
            else "MXN",
            "amount_cents": item.amount.cents if item.amount else None,
            "min_amount_cents": item.min_amount.cents if item.min_amount else None,
            "max_amount_cents": item.max_amount.cents if item.max_amount else None,
            "is_data_package": item.is_data_package,
            "validity_days": item.validity_days,
            "is_active": True,
            "last_seen_at": timezone.now(),
            "raw": item.raw,
        },
    )


def catalog_is_stale(*, max_age_hours: int = 24) -> bool:
    """Indica si el catalogo lleva demasiado sin sincronizarse.

    La UI lo usa para advertir al cajero. Vender con un catalogo de la semana
    pasada puede significar ofrecer un paquete que el operador ya retiro, y el
    fallo aparecerian despues de haber cobrado.
    """
    last = (
        CatalogSyncRun.objects.filter(succeeded=True).order_by("-finished_at").first()
    )
    if last is None or last.finished_at is None:
        return True
    age = timezone.now() - last.finished_at
    return age.total_seconds() > max_age_hours * 3600
