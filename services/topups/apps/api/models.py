"""Registro de idempotencia del servicio de recargas.

Ver ``samy_common.idempotency`` para el razonamiento. La tabla es propia del
servicio: compartirla entre microservicios seria acoplamiento de datos.
"""

from __future__ import annotations

from samy_common.idempotency.models import AbstractIdempotencyRecord


class IdempotencyRecord(AbstractIdempotencyRecord):
    class Meta(AbstractIdempotencyRecord.Meta):
        db_table = "topups_idempotency_record"
        verbose_name = "Registro de idempotencia"
        verbose_name_plural = "Registros de idempotencia"
