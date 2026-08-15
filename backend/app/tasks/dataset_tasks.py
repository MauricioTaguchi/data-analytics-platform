from pathlib import Path

from app.core.cache import CacheService
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.transformation import Transformation
from app.services.dataset_service import DatasetService
from app.services.storage_service import storage
from app.worker import celery_app


@celery_app.task(
    bind=True,
    name="datasets.import",
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    soft_time_limit=240,
    time_limit=300,
)
def import_dataset_task(self, dataset_id: int):
    db = SessionLocal()
    dataset = None
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            return {"status": "not_found", "dataset_id": dataset_id}
        if dataset.status == "ready":
            return {
                "status": "completed",
                "dataset_id": dataset.id,
                "rows": dataset.row_count,
                "columns": dataset.column_count,
                "version": dataset.version,
                "cached": True,
            }

        dataset.status = "processing"
        db.commit()
        self.update_state(state="PROGRESS", meta={"progress": 25, "stage": "validating"})
        dimensions = DatasetService.inspect_staged_dataset(dataset)
        self.update_state(state="PROGRESS", meta={"progress": 80, "stage": "persisting"})
        dataset.row_count = dimensions["rows"]
        dataset.column_count = dimensions["columns"]
        dataset.status = "ready"
        db.commit()
        return {
            "status": "completed",
            "dataset_id": dataset.id,
            "rows": dataset.row_count,
            "columns": dataset.column_count,
            "version": dataset.version,
            "progress": 100,
        }
    except OSError:
        db.rollback()
        if dataset:
            retryable = db.query(Dataset).filter(Dataset.id == dataset.id).first()
            if retryable:
                retryable.status = "queued"
                db.commit()
        raise
    except Exception:
        db.rollback()
        if dataset:
            failed = db.query(Dataset).filter(Dataset.id == dataset.id).first()
            if failed:
                failed.status = "failed"
                storage.delete(Path(failed.stored_path))
                db.commit()
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="datasets.profile",
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    soft_time_limit=240,
    time_limit=300,
)
def profile_dataset_task(self, dataset_id: int):
    db = SessionLocal()
    dataset = None
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            return {"status": "not_found", "dataset_id": dataset_id}
        if dataset.profile_json:
            self.update_state(state="PROGRESS", meta={"progress": 100, "stage": "cached"})
            return {"status": "completed", "dataset_id": dataset.id, "cached": True}
        if dataset.status != "profiling":
            DatasetService.ensure_ready(dataset)
        dataset.status = "profiling"
        db.commit()
        self.update_state(state="PROGRESS", meta={"progress": 15, "stage": "reading"})
        profile = DatasetService.build_profile(dataset)
        self.update_state(state="PROGRESS", meta={"progress": 80, "stage": "persisting"})
        dataset.profile_json = profile
        dataset.status = "profiled"
        db.commit()
        CacheService.set_json(f"dataset:{dataset.id}:profile", profile, ttl=1800)
        return {"status": "completed", "dataset_id": dataset.id, "progress": 100}
    except OSError:
        db.rollback()
        if dataset:
            retryable = db.query(Dataset).filter(Dataset.id == dataset.id).first()
            if retryable:
                retryable.status = "ready"
                db.commit()
        raise
    except Exception:
        db.rollback()
        if dataset:
            failed = db.query(Dataset).filter(Dataset.id == dataset.id).first()
            if failed:
                failed.status = "failed"
                db.commit()
        raise
    finally:
        CacheService.delete(f"dataset:{dataset_id}:active-profile-job")
        db.close()


@celery_app.task(
    bind=True,
    name="datasets.transform_preview",
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=180,
    time_limit=240,
)
def preview_transformation_task(
    self,
    dataset_id: int,
    operation: str,
    parameters: dict,
    expected_version: int,
):
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        if dataset.version != expected_version:
            raise ValueError("Dataset version changed before preview execution.")
        self.update_state(state="PROGRESS", meta={"progress": 20, "stage": "reading"})
        result = DatasetService.preview_transformation(dataset, operation, parameters)
        self.update_state(state="PROGRESS", meta={"progress": 100, "stage": "completed"})
        return result
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="datasets.transform",
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=240,
    time_limit=300,
)
def transform_dataset_task(self, transformation_id: int):
    db = SessionLocal()
    try:
        transformation = db.query(Transformation).filter(Transformation.id == transformation_id).first()
        if not transformation:
            return {"status": "not_found", "transformation_id": transformation_id}
        self.update_state(state="PROGRESS", meta={"progress": 20, "stage": "reading"})
        completed = DatasetService.execute_prepared_transformation(db, transformation_id)
        self.update_state(state="PROGRESS", meta={"progress": 100, "stage": "completed"})
        CacheService.delete(f"dataset:{completed.dataset_id}:profile")
        return {
            "status": "completed",
            "transformation_id": completed.id,
            "dataset_id": completed.dataset_id,
            "before_rows": completed.before_rows,
            "after_rows": completed.after_rows,
            "before_columns": completed.before_columns,
            "after_columns": completed.after_columns,
            "progress": 100,
        }
    finally:
        db.close()
