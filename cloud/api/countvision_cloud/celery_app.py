"""Celery: background worker (emails now; reports, video jobs, alerts later) and beat (timers).

Worker:  celery -A countvision_cloud.celery_app worker --loglevel=INFO
Beat:    celery -A countvision_cloud.celery_app beat --loglevel=INFO
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from .settings import get_settings

_settings = get_settings()

celery = Celery("countvision", broker=_settings.redis_url, backend=None, include=["countvision_cloud.tasks"])
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,  # a task is only removed after it finished (worker crash -> retried)
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
    beat_schedule={
        "cleanup-expired": {"task": "countvision_cloud.tasks.cleanup", "schedule": crontab(minute=17)},
        "beat-heartbeat": {"task": "countvision_cloud.tasks.heartbeat", "schedule": 60.0},
    },
)

app = celery  # "celery -A countvision_cloud.celery_app" finds this
