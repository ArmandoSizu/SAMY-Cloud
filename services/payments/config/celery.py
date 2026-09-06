"""Configuracion de Celery del microservicio de pagos.

Tareas periodicas y su razon de ser:

* ``drain_outbox`` (cada 5 s) - publica al bus los eventos pendientes. Es lo
  que convierte el outbox transaccional en entrega real.
* ``expire_stale_orders`` (cada minuto) - cierra intentos de cobro vencidos,
  consultando antes al proveedor por si el pago entro en el ultimo momento.
* ``reconcile_indeterminate`` (cada 2 min) - resuelve operaciones cuyo
  resultado quedo desconocido tras un timeout. Es el mecanismo que impide que
  una orden se quede colgada para siempre.
* ``purge_idempotency`` (diaria) - limpia registros de idempotencia vencidos.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

app = Celery("payments")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Cierra la cadena PAID -> PROCESSING -> SUCCESS leyendo los resultados
    # que publican los servicios ejecutores. Sin esta tarea la orden se queda
    # en PAID aunque la recarga ya se haya entregado.
    "consume-fulfillment-events": {
        "task": "apps.orders.consumers.consume_fulfillment_events",
        "schedule": 5.0,
        "options": {"expires": 20},
    },
    "drain-outbox": {
        "task": "apps.outbox.tasks.drain_outbox",
        "schedule": 5.0,
        # expires evita que se acumule una cola de tareas identicas si el
        # worker estuvo caido: al volver, ejecuta una, no doscientas.
        "options": {"expires": 20},
    },
    "expire-stale-orders": {
        "task": "apps.orders.tasks.expire_stale_orders",
        "schedule": 60.0,
        "options": {"expires": 55},
    },
    "reconcile-indeterminate": {
        "task": "apps.orders.tasks.reconcile_indeterminate_orders",
        "schedule": 120.0,
        "options": {"expires": 110},
    },
    # Cierra el hueco entre las otras dos barridas: cobros aceptados por el
    # proveedor cuyo webhook no llego. Es la pata de "consulta" del modelo de
    # verdad (respuesta inmediata + consulta + webhook firmado), y la unica
    # que funciona cuando el webhook no puede llegar, como en local.
    "reconcile-pending-payments": {
        "task": "apps.orders.tasks.reconcile_pending_payments",
        "schedule": 120.0,
        "options": {"expires": 110},
    },
    "purge-idempotency": {
        "task": "apps.api.tasks.purge_expired_idempotency",
        "schedule": crontab(hour=4, minute=30),
    },
}
