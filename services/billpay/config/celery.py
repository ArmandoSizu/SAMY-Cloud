"""Celery del microservicio de pago de servicios."""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

app = Celery("billpay")
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
    "reconcile-pending-payments": {
        "task": "apps.fulfillment.tasks.reconcile_pending_payments",
        "schedule": 120.0,
        "options": {"expires": 110},
    },
}
