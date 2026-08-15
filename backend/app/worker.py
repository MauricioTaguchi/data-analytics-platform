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
    worker_max_memory_per_child=settings.CELERY_WORKER_MAX_MEMORY_KB,
    worker_max_tasks_per_child=settings.CELERY_WORKER_MAX_TASKS,
    beat_schedule={
        "remove-orphaned-storage-files": {
            "task": "storage.remove_orphans",
            "schedule": 3_600.0,
        },
    },
)
