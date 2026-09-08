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
    # The durable outbox owns retries. A publish must return promptly so an
    # unavailable broker cannot strand the database recovery process.
    task_publish_retry=False,
    broker_connection_timeout=5,
    broker_transport_options={
        "max_retries": 0,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "retry_on_timeout": False,
    },
    result_expires=3_600,
    worker_max_memory_per_child=settings.CELERY_WORKER_MAX_MEMORY_KB,
    worker_max_tasks_per_child=settings.CELERY_WORKER_MAX_TASKS,
    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,
)
