"""Celery application for stripe-saas-django."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

app = Celery("stripe_saas_django")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
