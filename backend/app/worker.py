from celery import Celery
from app.core.config import settings

broker_url = "memory://" if settings.CELERY_EAGER else settings.REDIS_URL
result_backend = "cache+memory://" if settings.CELERY_EAGER else settings.REDIS_URL

celery_app = Celery(
    "data_analytics_platform",
    broker=broker_url,
    backend=result_backend,
    include=[
        "app.tasks.dataset_tasks",
        "app.tasks.report_tasks",
        "app.tasks.maintenance_tasks",
        "app.tasks.outbox_tasks",
        "app.tasks.retention_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Sao_Paulo",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_always_eager=settings.CELERY_EAGER,
    task_store_eager_result=settings.CELERY_EAGER,
    task_ignore_result=not settings.CELERY_EAGER,
    task_store_errors_even_if_ignored=False,
    result_expires=3_600,
    worker_max_memory_per_child=settings.CELERY_WORKER_MAX_MEMORY_KB,
    worker_max_tasks_per_child=settings.CELERY_WORKER_MAX_TASKS,
    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,
    beat_schedule={
        "remove-orphaned-storage-files": {
            "task": "storage.remove_orphans",
            "schedule": 3_600.0,
        },
        "remove-expired-refresh-sessions": {
            "task": "auth.remove_expired_refresh_sessions",
            "schedule": 3_600.0,
        },
        "dispatch-pending-jobs": {
            "task": "jobs.dispatch_pending",
            "schedule": 10.0,
        },
        "remove-expired-job-history": {
            "task": "jobs.remove_expired_history",
            "schedule": 86_400.0,
        },
    },
)
