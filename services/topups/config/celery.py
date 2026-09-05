"""Celery del microservicio de recargas.

Tareas periodicas:

* ``consume_payment_events`` (cada 5 s) - lee el stream de eventos del
  servicio de Pagos y encola las recargas cuya orden ya quedo pagada. Es el
  disparador de toda ejecucion.
* ``drain_outbox`` (cada 5 s) - publica los resultados de vuelta a Pagos.
* ``sync_catalog`` (cada 6 h) - refresca operadores y denominaciones desde el
  proveedor. Sin esto, el catalogo envejece y se ofrecen paquetes retirados.
* ``reconcile_pending`` (cada 2 min) - resuelve recargas cuyo resultado quedo
  desconocido, consultando al proveedor por su clave de idempotencia.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

app = Celery("topups")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "consume-payment-events": {
        "task": "apps.fulfillment.tasks.consume_payment_events",
        "schedule": 5.0,
        "options": {"expires": 20},
    },
    "drain-outbox": {
        "task": "apps.outbox.tasks.drain_outbox",
        "schedule": 5.0,
        "options": {"expires": 20},
    },
    "sync-catalog": {
        "task": "apps.catalog.tasks.sync_catalog_task",
        # El intervalo real se toma de CATALOG_SYNC_HOURS; 6 h por defecto.
        "schedule": 6 * 60 * 60.0,
        "options": {"expires": 3600},
    },
    "reconcile-pending-topups": {
        "task": "apps.fulfillment.tasks.reconcile_pending_topups",
        "schedule": 120.0,
        "options": {"expires": 110},
    },
}
