"""Resolucion y calculo de comisiones.

Este modulo responde a una sola pregunta:

    Dado (tienda, servicio, producto, monto base),
    ¿cuanto se le cobra al cliente y como se reparte?

Puntos de diseno:

* **Siempre devuelve un resultado.** Si no hay ninguna regla configurada
  aplica la regla por defecto de la plataforma. Una venta nunca se cae por
  falta de configuracion de comisiones.

* **El calculo se congela en la orden.** El resultado se guarda en la orden en
  el momento de crearla, junto con el ID de la regla usada. Si manana el dueno
  cambia el porcentaje, las ordenes de ayer siguen mostrando lo que
  efectivamente se cobro. Recalcular una comision historica es falsear la
  contabilidad.

* **El monto base es explicito.** Para una recarga de $100 con comision de $5,
  el cliente paga $105 y el proveedor recibe $100. El base es siempre el valor
  del servicio, nunca el total cobrado, para que la comision no se calcule
  sobre si misma.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import structlog
from django.db.models import Q

from apps.commissions.models import (
    CommissionRule,
    CommissionSplit,
    CommissionType,
    ServiceKind,
)
from samy_common.money import Money

log = structlog.get_logger("commissions")


@dataclass(frozen=True, slots=True)
class CommissionResult:
    """Resultado del calculo. Es lo que se congela en la orden."""

    #: Valor del servicio (lo que recibe el proveedor).
    base: Money
    #: Comision cobrada al cliente.
    commission: Money
    #: Total que paga el cliente = base + comision.
    total: Money

    #: Reparto de la comision.
    store_share: Money
    platform_share: Money
    provider_share: Money

    #: Trazabilidad: que regla se aplico y como estaba configurada.
    rule_id: uuid.UUID | None
    rule_name: str
    rule_description: str

    def as_dict(self) -> dict[str, object]:
        """Serializacion para guardar en la orden y mostrar en la UI."""
        return {
            "base_cents": self.base.cents,
            "commission_cents": self.commission.cents,
            "total_cents": self.total.cents,
            "currency": self.base.currency,
            "store_share_cents": self.store_share.cents,
            "platform_share_cents": self.platform_share.cents,
            "provider_share_cents": self.provider_share.cents,
            "rule_id": str(self.rule_id) if self.rule_id else None,
            "rule_name": self.rule_name,
            "rule_description": self.rule_description,
        }


#: Regla usada cuando no hay ninguna configurada. Es explicita y visible en el
#: comprobante, no un cero silencioso: el dueno debe darse cuenta de que aun
#: no configuro sus comisiones.
DEFAULT_RULE_NAME = "Regla por defecto de la plataforma"
DEFAULT_FIXED_CENTS = 0
DEFAULT_PERCENTAGE = Decimal("0.000")


def resolve_rule(
    *,
    service_kind: str,
    store_id: uuid.UUID | str | None,
    organization_id: uuid.UUID | str | None = None,
    product_code: str = "",
) -> CommissionRule | None:
    """Encuentra la regla mas especifica aplicable.

    Orden de especificidad (mayor gana):
        producto (4) > tienda (2) > organizacion (1) > plataforma (0)

    Se resuelve en Python y no con SQL porque la puntuacion depende de que
    campos son nulos; expresarlo en SQL daria una consulta ilegible sin ganar
    rendimiento: el conjunto candidato es de unas pocas filas.
    """
    candidates = CommissionRule.objects.filter(
        service_kind=service_kind, is_active=True
    ).select_related("split")

    scope = Q(store_id__isnull=True, organization_id__isnull=True)
    if organization_id:
        scope |= Q(organization_id=organization_id)
    if store_id:
        scope |= Q(store_id=store_id)
    candidates = candidates.filter(scope)

    # El producto acota, no amplia: una regla con product_code solo aplica a
    # ese producto; una sin product_code aplica a todos.
    if product_code:
        candidates = candidates.filter(Q(product_code="") | Q(product_code=product_code))
    else:
        candidates = candidates.filter(product_code="")

    applicable = [rule for rule in candidates if rule.is_currently_valid]
    if not applicable:
        return None

    applicable.sort(key=lambda r: (r.specificity, r.priority, r.created_at), reverse=True)
    return applicable[0]


def calculate(
    *,
    base: Money,
    service_kind: str,
    store_id: uuid.UUID | str | None,
    organization_id: uuid.UUID | str | None = None,
    product_code: str = "",
) -> CommissionResult:
    """Calcula comision y reparto para una operacion concreta."""
    if base.cents < 0:
        raise ValueError("El monto base de una comision no puede ser negativo.")

    rule = resolve_rule(
        service_kind=service_kind,
        store_id=store_id,
        organization_id=organization_id,
        product_code=product_code,
    )

    if rule is None:
        log.info(
            "commission_rule_not_found_using_default",
            service_kind=service_kind,
            store_id=str(store_id) if store_id else None,
            product_code=product_code,
        )
        zero = Money.zero(base.currency)
        return CommissionResult(
            base=base,
            commission=zero,
            total=base,
            store_share=zero,
            platform_share=zero,
            provider_share=zero,
            rule_id=None,
            rule_name=DEFAULT_RULE_NAME,
            rule_description="Sin comision configurada para este servicio.",
        )

    commission = rule.calculate(base)

    split = getattr(rule, "split", None)
    if split is None:
        # Regla sin reparto configurado: toda la comision queda en la tienda.
        # Es el comportamiento menos sorprendente para un dueno que apenas
        # esta configurando su negocio.
        shares = {
            "store": commission,
            "platform": Money.zero(base.currency),
            "provider": Money.zero(base.currency),
        }
    else:
        shares = split.allocate(commission)

    result = CommissionResult(
        base=base,
        commission=commission,
        total=base + commission,
        store_share=shares["store"],
        platform_share=shares["platform"],
        provider_share=shares["provider"],
        rule_id=rule.id,
        rule_name=rule.name,
        rule_description=rule.describe(),
    )

    # Invariante contable: el reparto debe sumar exactamente la comision.
    # Se comprueba en cada calculo, no solo en las pruebas: un descuadre aqui
    # significa dinero que aparece o desaparece.
    total_shares = (
        result.store_share.cents
        + result.platform_share.cents
        + result.provider_share.cents
    )
    if total_shares != commission.cents:  # pragma: no cover - invariante
        raise AssertionError(
            f"Descuadre en el reparto de comision: partes suman {total_shares}, "
            f"comision es {commission.cents}. Regla {rule.id}."
        )

    return result


def preview_for_store(
    *, store_id: uuid.UUID | str, service_kind: str, amounts: list[Money]
) -> list[dict[str, object]]:
    """Vista previa de comisiones para varios montos.

    Se usa en la pantalla de configuracion, para que el dueno vea el efecto
    real de una regla antes de guardarla, en vez de tener que deducirlo.
    """
    return [
        calculate(base=amount, service_kind=service_kind, store_id=store_id).as_dict()
        for amount in amounts
    ]
