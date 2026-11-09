"""Celery application for the free/open-source DataSight worker runtime."""

from __future__ import annotations

from celery import Celery

from datasight.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND, CELERY_TASK_ALWAYS_EAGER


celery_app = Celery(
    "datasight_pipeline",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=["datasight.infrastructure.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_always_eager=CELERY_TASK_ALWAYS_EAGER,
    task_routes={
        "datasight.extract_features": {"queue": "llm"},
        "datasight.resume_extractions": {"queue": "llm"},
        "datasight.bootstrap_high_throughput_run": {"queue": "processing"},
        "datasight.dispatch_high_throughput_run": {"queue": "processing"},
        "datasight.process_high_throughput_stage": {"queue": "processing"},
        "datasight.finalize_high_throughput_run": {"queue": "export"},
    },
)
