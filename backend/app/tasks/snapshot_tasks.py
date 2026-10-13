from app.services.snapshot_service import SnapshotService
from app.worker import celery_app


@celery_app.task(name="datasets.expire_snapshots", soft_time_limit=60, time_limit=90)
def expire_dataset_snapshots():
    return SnapshotService.expire_history()
