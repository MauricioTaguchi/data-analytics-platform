from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.report import Report
from app.models.transformation import Transformation
from app.services.storage_service import storage
from app.worker import celery_app


def _is_older_than(value: datetime | None, cutoff: datetime) -> bool:
    if value is None:
        return False
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized < cutoff


@celery_app.task(name="storage.remove_orphans", soft_time_limit=120, time_limit=180)
def remove_orphaned_storage_files(grace_hours: int = 24):
    """Reconcile stale jobs and remove files not referenced by durable state."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(grace_hours, 1))
        datasets = db.query(Dataset).all()
        referenced = {str(Path(dataset.stored_path).resolve()) for dataset in datasets if dataset.stored_path}
        reconciled_transformations = 0
        for transformation in db.query(Transformation).all():
            if transformation.input_path:
                referenced.add(str(Path(transformation.input_path).resolve()))
            if transformation.status in {"completed", "undone"} and transformation.output_path:
                referenced.add(str(Path(transformation.output_path).resolve()))
            elif transformation.status in {"pending", "processing"}:
                if _is_older_than(transformation.created_at, cutoff):
                    transformation.status = "failed"
                    transformation.error_message = "Reconciled after the worker lease expired."
                    dataset = db.query(Dataset).filter(Dataset.id == transformation.dataset_id).first()
                    if dataset and dataset.version == transformation.expected_version:
                        dataset.status = "ready"
                    reconciled_transformations += 1
                elif transformation.output_path:
                    referenced.add(str(Path(transformation.output_path).resolve()))

        reconciled_reports = 0
        for report in db.query(Report).all():
            if report.file_path:
                referenced.add(str(Path(report.file_path).resolve()))
            if report.status in {"queued", "processing"} and _is_older_than(report.created_at, cutoff):
                report.status = "failed"
                report.error_message = "Reconciled after the worker lease expired."
                reconciled_reports += 1

        reconciled_datasets = 0
        active_dataset_ids = {
            transformation.dataset_id
            for transformation in db.query(Transformation).filter(
                Transformation.status.in_({"pending", "processing"})
            )
            if not _is_older_than(transformation.created_at, cutoff)
        }
        for dataset in datasets:
            if not _is_older_than(dataset.updated_at or dataset.created_at, cutoff):
                continue
            if dataset.status in {"queued", "processing"}:
                dataset.status = "failed"
                reconciled_datasets += 1
            elif dataset.status == "profiling":
                dataset.status = "ready"
                reconciled_datasets += 1
            elif dataset.status == "transforming" and dataset.id not in active_dataset_ids:
                dataset.status = "ready"
                reconciled_datasets += 1
        db.commit()

        removed = []
        for root in {storage.root, Path(settings.REPORT_DIR)}:
            root.mkdir(parents=True, exist_ok=True)
            for path in root.iterdir():
                if not path.is_file() or str(path.resolve()) in referenced:
                    continue
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if modified < cutoff:
                    storage.delete(path)
                    removed.append(path.name)
        return {
            "status": "completed",
            "removed": removed,
            "count": len(removed),
            "reconciled_transformations": reconciled_transformations,
            "reconciled_reports": reconciled_reports,
            "reconciled_datasets": reconciled_datasets,
        }
    finally:
        db.close()
