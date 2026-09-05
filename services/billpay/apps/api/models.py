"""Registro de idempotencia del servicio de pago de servicios.

Ver ``samy_common.idempotency`` para el razonamiento. La tabla es propia del
servicio: compartirla entre microservicios seria acoplamiento de datos.
"""

from __future__ import annotations

from samy_common.idempotency.models import (
    AbstractIdempotencyRecord,
    IdempotencyStatus,
)

# Se reexporta para que el resto del servicio importe ambos desde aqui y no
# tenga que conocer la ruta interna del paquete compartido.
__all__ = ["IdempotencyRecord", "IdempotencyStatus"]


class IdempotencyRecord(AbstractIdempotencyRecord):
    class Meta(AbstractIdempotencyRecord.Meta):
        db_table = "billpay_idempotency_record"
        verbose_name = "Registro de idempotencia"
        verbose_name_plural = "Registros de idempotencia"
