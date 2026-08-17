import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.core.cache import CacheService
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.transformation import Transformation
from app.services.dataset_service import DatasetService
from app.services.job_service import (
    CANCELLATION_JOB_STATUSES,
    JobCancellationRequested,
    JobLeaseUnavailable,
    JobService,
    JobStateConflict,
)
from app.services.storage_service import storage
from app.worker import celery_app


logger = logging.getLogger("dataflow.jobs")


def _cancellation_pending(db, task_id: str) -> bool:
    job = JobService.get(db, task_id)
    return bool(job and job.status in CANCELLATION_JOB_STATUSES)


def _retry_exhausted(task) -> bool:
    return task.max_retries is not None and task.request.retries >= task.max_retries


def _lease_checkpoint(db, task_id: str, attempt_token: str) -> None:
    """Renew a job lease in its own short transaction."""
    try:
        JobService.ensure_active(
            db,
            task_id,
            attempt_token=attempt_token,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _recover_transformation_finalization(
    task_id: str,
    attempt_token: str,
    transformation_id: int,
    error_message: str,
) -> str:
    """Resolve a failed or acknowledgement-ambiguous final commit safely."""
    recovery = SessionLocal()
    output_path: Path | None = None
    outcome = "failed"
    try:
        job = JobService.get(recovery, task_id)
        transformation = (
            recovery.query(Transformation)
            .execution_options(populate_existing=True)
            .filter(Transformation.id == transformation_id)
            .first()
        )
        dataset = None
        if transformation is not None:
            dataset = (
                recovery.query(Dataset)
                .execution_options(populate_existing=True)
                .filter(Dataset.id == transformation.dataset_id)
                .first()
            )
        if (
            job is not None
            and job.status == "SUCCESS"
            and transformation is not None
            and transformation.status == "completed"
            and dataset is not None
            and dataset.stored_path == transformation.output_path
            and dataset.version == transformation.expected_version + 1
        ):
            recovery.rollback()
            return "completed"

        cancellation_requested = bool(
            job is not None and job.status == "CANCELLATION_REQUESTED"
        )
        if cancellation_requested:
            transitioned = JobService.cancel(
                recovery,
                task_id,
                attempt_token=attempt_token,
                enforce_attempt=True,
            )
            outcome = "cancelled"
        else:
            transitioned = JobService.fail(
                recovery,
                task_id,
                error_message,
                attempt_token=attempt_token,
            )
        if not transitioned:
            recovery.rollback()
            return "superseded"

        if transformation is not None:
            output_path = Path(transformation.output_path)
            transformation.status = "cancelled" if cancellation_requested else "failed"
            transformation.error_message = None if cancellation_requested else error_message[:2_000]
            if (
                dataset is not None
                and dataset.version == transformation.expected_version
                and dataset.stored_path == transformation.input_path
            ):
                dataset.status = "ready"
        recovery.commit()
    except Exception:
        recovery.rollback()
        logger.exception(
            "Could not reconcile transformation %s after finalization failed.",
            transformation_id,
        )
        return "unknown"
    finally:
        recovery.close()

    if output_path is not None:
        try:
            storage.delete(output_path)
        except OSError:
            logger.warning(
                "Failed transformation artifact %s will be retried by orphan cleanup.",
                output_path,
            )
    return outcome


@celery_app.task(
    bind=True,
    name="datasets.import",
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=3,
    soft_time_limit=240,
    time_limit=300,
)
def import_dataset_task(self, dataset_id: int):
    task_id = str(self.request.id)
    attempt_token = uuid4().hex
    db = SessionLocal()
    try:
        job, acquired = JobService.start(
            db,
            task_id,
            attempt_token=attempt_token,
            stage="loading",
        )
        if not acquired:
            return job.result_json or {"status": job.status.lower()}
        db.commit()
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise ValueError("Dataset no longer exists.")
        if dataset.status == "ready":
            result = {
                "status": "completed",
                "dataset_id": dataset.id,
                "rows": dataset.row_count,
                "columns": dataset.column_count,
                "version": dataset.version,
                "cached": True,
            }
            JobService.succeed(db, task_id, result, attempt_token=attempt_token)
            db.commit()
            return result

        dataset.status = "processing"
        JobService.progress(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=25,
            stage="validating",
        )
        dataset_snapshot = SimpleNamespace(stored_path=str(dataset.stored_path))
        db.commit()
        dimensions = DatasetService.inspect_staged_dataset(dataset_snapshot)
        _lease_checkpoint(db, task_id, attempt_token)
        dataset = (
            db.query(Dataset)
            .execution_options(populate_existing=True)
            .filter(Dataset.id == dataset_id)
            .first()
        )
        if not dataset:
            raise ValueError("Dataset no longer exists during import finalization.")
        JobService.progress(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=80,
            stage="persisting",
        )
        dataset.row_count = dimensions["rows"]
        dataset.column_count = dimensions["columns"]
        dataset.status = "ready"
        result = {
            "status": "completed",
            "dataset_id": dataset.id,
            "rows": dataset.row_count,
            "columns": dataset.column_count,
            "version": dataset.version,
            "progress": 100,
        }
        JobService.succeed(db, task_id, result, attempt_token=attempt_token)
        db.commit()
        return result
    except JobCancellationRequested:
        db.rollback()
        if not JobService.cancel(
            db,
            task_id,
            attempt_token=attempt_token,
            enforce_attempt=True,
        ):
            db.rollback()
            return {"status": "superseded", "dataset_id": dataset_id}
        cancelled = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        cancelled_path: Path | None = None
        if cancelled and cancelled.status not in {"ready", "profiled"}:
            cancelled.status = "cancelled"
            cancelled.deleted_at = datetime.now(timezone.utc)
            cancelled_path = Path(cancelled.stored_path)
        db.commit()
        if cancelled_path:
            storage.delete(cancelled_path)
        return {"status": "cancelled", "dataset_id": dataset_id}
    except JobLeaseUnavailable as exc:
        db.rollback()
        raise self.retry(
            exc=exc,
            countdown=exc.retry_after_seconds,
            max_retries=10,
        ) from exc
    except JobStateConflict:
        db.rollback()
        return {"status": "superseded", "dataset_id": dataset_id}
    except OSError as exc:
        db.rollback()
        if _cancellation_pending(db, task_id):
            if not JobService.cancel(
                db,
                task_id,
                attempt_token=attempt_token,
                enforce_attempt=True,
            ):
                db.rollback()
                return {"status": "superseded", "dataset_id": dataset_id}
            cancelled = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            cancelled_path: Path | None = None
            if cancelled and cancelled.status not in {"ready", "profiled"}:
                cancelled.status = "cancelled"
                cancelled.deleted_at = datetime.now(timezone.utc)
                cancelled_path = Path(cancelled.stored_path)
            db.commit()
            if cancelled_path:
                storage.delete(cancelled_path)
            return {"status": "cancelled", "dataset_id": dataset_id}
        retryable = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        failed_path: Path | None = None
        if _retry_exhausted(self):
            transitioned = JobService.fail(
                db,
                task_id,
                str(exc),
                attempt_token=attempt_token,
            )
            if not transitioned:
                db.rollback()
                return {"status": "superseded", "dataset_id": dataset_id}
            if retryable:
                retryable.status = "failed"
                failed_path = Path(retryable.stored_path)
        else:
            transitioned = JobService.retry(db, task_id, str(exc), attempt_token=attempt_token)
            if not transitioned:
                db.rollback()
                return {"status": "superseded", "dataset_id": dataset_id}
            if retryable:
                retryable.status = "queued"
        db.commit()
        if failed_path:
            storage.delete(failed_path)
        raise
    except Exception as exc:
        db.rollback()
        failed = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        failed_path = None
        transitioned = JobService.fail(
            db,
            task_id,
            str(exc),
            attempt_token=attempt_token,
        )
        if not transitioned:
            db.rollback()
            return {"status": "superseded", "dataset_id": dataset_id}
        if failed:
            failed.status = "failed"
            failed_path = Path(failed.stored_path)
        db.commit()
        if failed_path:
            storage.delete(failed_path)
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="datasets.profile",
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=3,
    soft_time_limit=240,
    time_limit=300,
)
def profile_dataset_task(self, dataset_id: int):
    task_id = str(self.request.id)
    attempt_token = uuid4().hex
    db = SessionLocal()
    try:
        job, acquired = JobService.start(
            db,
            task_id,
            attempt_token=attempt_token,
            stage="loading",
        )
        if not acquired:
            return job.result_json or {"status": job.status.lower()}
        db.commit()
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise ValueError("Dataset no longer exists.")
        if dataset.profile_json:
            result = {"status": "completed", "dataset_id": dataset.id, "cached": True}
            JobService.succeed(db, task_id, result, attempt_token=attempt_token)
            db.commit()
            return result
        if dataset.status != "profiling":
            DatasetService.ensure_ready(dataset)
        dataset.status = "profiling"
        JobService.progress(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=15,
            stage="reading",
        )
        dataset_snapshot = SimpleNamespace(stored_path=str(dataset.stored_path))
        db.commit()
        profile = DatasetService.build_profile(dataset_snapshot)
        JobService.ensure_result_size(profile, label="Dataset profile")
        _lease_checkpoint(db, task_id, attempt_token)
        dataset = (
            db.query(Dataset)
            .execution_options(populate_existing=True)
            .filter(Dataset.id == dataset_id)
            .first()
        )
        if not dataset:
            raise ValueError("Dataset no longer exists during profile finalization.")
        JobService.progress(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=80,
            stage="persisting",
        )
        dataset.profile_json = profile
        dataset.status = "profiled"
        result = {"status": "completed", "dataset_id": dataset.id, "progress": 100}
        JobService.succeed(db, task_id, result, attempt_token=attempt_token)
        db.commit()
        CacheService.set_json(f"dataset:{dataset_id}:profile", profile, ttl=1800)
        return result
    except JobCancellationRequested:
        db.rollback()
        if not JobService.cancel(
            db,
            task_id,
            attempt_token=attempt_token,
            enforce_attempt=True,
        ):
            db.rollback()
            return {"status": "superseded", "dataset_id": dataset_id}
        cancelled = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if cancelled and cancelled.status == "profiling":
            cancelled.status = "ready"
        db.commit()
        return {"status": "cancelled", "dataset_id": dataset_id}
    except JobLeaseUnavailable as exc:
        db.rollback()
        raise self.retry(
            exc=exc,
            countdown=exc.retry_after_seconds,
            max_retries=10,
        ) from exc
    except JobStateConflict:
        db.rollback()
        return {"status": "superseded", "dataset_id": dataset_id}
    except OSError as exc:
        db.rollback()
        if _cancellation_pending(db, task_id):
            if not JobService.cancel(
                db,
                task_id,
                attempt_token=attempt_token,
                enforce_attempt=True,
            ):
                db.rollback()
                return {"status": "superseded", "dataset_id": dataset_id}
            cancelled = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if cancelled and cancelled.status == "profiling":
                cancelled.status = "ready"
            db.commit()
            return {"status": "cancelled", "dataset_id": dataset_id}
        retryable = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if _retry_exhausted(self):
            transitioned = JobService.fail(
                db,
                task_id,
                str(exc),
                attempt_token=attempt_token,
            )
            if not transitioned:
                db.rollback()
                return {"status": "superseded", "dataset_id": dataset_id}
            if retryable and retryable.status == "profiling":
                retryable.status = "ready"
        else:
            transitioned = JobService.retry(db, task_id, str(exc), attempt_token=attempt_token)
            if not transitioned:
                db.rollback()
                return {"status": "superseded", "dataset_id": dataset_id}
            if retryable:
                retryable.status = "ready"
        db.commit()
        raise
    except Exception as exc:
        db.rollback()
        failed = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        transitioned = JobService.fail(
            db,
            task_id,
            str(exc),
            attempt_token=attempt_token,
        )
        if not transitioned:
            db.rollback()
            return {"status": "superseded", "dataset_id": dataset_id}
        if failed and failed.status == "profiling":
            failed.status = "ready"
        db.commit()
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="datasets.transform_preview",
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=2,
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
    task_id = str(self.request.id)
    attempt_token = uuid4().hex
    db = SessionLocal()
    try:
        job, acquired = JobService.start(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=20,
            stage="reading",
        )
        if not acquired:
            return job.result_json or {"status": job.status.lower()}
        db.commit()
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise ValueError("Dataset not found.")
        if dataset.version != expected_version:
            raise ValueError("Dataset version changed before preview execution.")
        stored_path = str(dataset.stored_path)
        dataset_snapshot = SimpleNamespace(status=str(dataset.status), stored_path=stored_path)
        db.commit()
        result = DatasetService.preview_transformation(dataset_snapshot, operation, parameters)
        _lease_checkpoint(db, task_id, attempt_token)
        current_dataset = (
            db.query(Dataset)
            .execution_options(populate_existing=True)
            .filter(Dataset.id == dataset_id)
            .first()
        )
        if (
            not current_dataset
            or current_dataset.version != expected_version
            or current_dataset.stored_path != stored_path
        ):
            raise ValueError("Dataset changed while the transformation preview was being built.")
        JobService.succeed(db, task_id, result, attempt_token=attempt_token)
        db.commit()
        return result
    except JobCancellationRequested:
        db.rollback()
        if not JobService.cancel(
            db,
            task_id,
            attempt_token=attempt_token,
            enforce_attempt=True,
        ):
            db.rollback()
            return {"status": "superseded", "dataset_id": dataset_id}
        db.commit()
        return {"status": "cancelled", "dataset_id": dataset_id}
    except JobLeaseUnavailable as exc:
        db.rollback()
        raise self.retry(
            exc=exc,
            countdown=exc.retry_after_seconds,
            max_retries=10,
        ) from exc
    except JobStateConflict:
        db.rollback()
        return {"status": "superseded", "dataset_id": dataset_id}
    except OSError as exc:
        db.rollback()
        if _cancellation_pending(db, task_id):
            if not JobService.cancel(
                db,
                task_id,
                attempt_token=attempt_token,
                enforce_attempt=True,
            ):
                db.rollback()
                return {"status": "superseded", "dataset_id": dataset_id}
            db.commit()
            return {"status": "cancelled", "dataset_id": dataset_id}
        if _retry_exhausted(self):
            transitioned = JobService.fail(
                db,
                task_id,
                str(exc),
                attempt_token=attempt_token,
            )
        else:
            transitioned = JobService.retry(db, task_id, str(exc), attempt_token=attempt_token)
        if not transitioned:
            db.rollback()
            return {"status": "superseded", "dataset_id": dataset_id}
        db.commit()
        raise
    except Exception as exc:
        db.rollback()
        transitioned = JobService.fail(
            db,
            task_id,
            str(exc),
            attempt_token=attempt_token,
        )
        if not transitioned:
            db.rollback()
            return {"status": "superseded", "dataset_id": dataset_id}
        db.commit()
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="datasets.transform",
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
def transform_dataset_task(self, transformation_id: int):
    task_id = str(self.request.id)
    attempt_token = uuid4().hex
    db = SessionLocal()
    completed: Transformation | None = None
    completed_dataset_id: int | None = None
    result: dict | None = None
    try:
        job, acquired = JobService.start(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=20,
            stage="reading",
        )
        if not acquired:
            return job.result_json or {"status": job.status.lower()}
        db.commit()
        transformation = db.query(Transformation).filter(Transformation.id == transformation_id).first()
        if not transformation:
            raise ValueError("Transformation no longer exists.")
        completed = DatasetService.execute_prepared_transformation(
            db,
            transformation_id,
            checkpoint=lambda: _lease_checkpoint(db, task_id, attempt_token),
            transaction_fence=lambda: JobService.progress(
                db,
                task_id,
                attempt_token=attempt_token,
                progress=90,
                stage="persisting",
            ),
        )
        completed_dataset_id = int(completed.dataset_id)
        result = {
            "status": "completed",
            "transformation_id": completed.id,
            "dataset_id": completed.dataset_id,
            "before_rows": completed.before_rows,
            "after_rows": completed.after_rows,
            "before_columns": completed.before_columns,
            "after_columns": completed.after_columns,
            "progress": 100,
        }
        JobService.succeed(db, task_id, result, attempt_token=attempt_token)
        db.commit()
        CacheService.delete(f"dataset:{completed_dataset_id}:profile")
        return result
    except JobCancellationRequested:
        db.rollback()
        if not JobService.cancel(
            db,
            task_id,
            attempt_token=attempt_token,
            enforce_attempt=True,
        ):
            db.rollback()
            return {"status": "superseded", "transformation_id": transformation_id}
        cancelled = db.query(Transformation).filter(Transformation.id == transformation_id).first()
        cancelled_path: Path | None = None
        if cancelled:
            cancelled_path = Path(cancelled.output_path)
            cancelled.status = "cancelled"
            cancelled.error_message = None
            dataset = db.query(Dataset).filter(Dataset.id == cancelled.dataset_id).first()
            if dataset and dataset.version == cancelled.expected_version:
                dataset.status = "ready"
        db.commit()
        if cancelled_path:
            storage.delete(cancelled_path)
        return {"status": "cancelled", "transformation_id": transformation_id}
    except JobLeaseUnavailable as exc:
        db.rollback()
        raise self.retry(
            exc=exc,
            countdown=exc.retry_after_seconds,
            max_retries=10,
        ) from exc
    except JobStateConflict:
        db.rollback()
        if completed is not None:
            outcome = _recover_transformation_finalization(
                task_id,
                attempt_token,
                transformation_id,
                "The worker attempt lost ownership while finalizing the transformation.",
            )
            if outcome == "completed" and result is not None:
                CacheService.delete(f"dataset:{completed_dataset_id}:profile")
                return result
            if outcome == "cancelled":
                return {"status": "cancelled", "transformation_id": transformation_id}
        return {"status": "superseded", "transformation_id": transformation_id}
    except OSError as exc:
        db.rollback()
        if completed is not None:
            outcome = _recover_transformation_finalization(
                task_id,
                attempt_token,
                transformation_id,
                str(exc),
            )
            if outcome == "completed" and result is not None:
                CacheService.delete(f"dataset:{completed_dataset_id}:profile")
                return result
            if outcome in {"cancelled", "superseded"}:
                return {"status": outcome, "transformation_id": transformation_id}
            raise
        if _cancellation_pending(db, task_id):
            if not JobService.cancel(
                db,
                task_id,
                attempt_token=attempt_token,
                enforce_attempt=True,
            ):
                db.rollback()
                return {"status": "superseded", "transformation_id": transformation_id}
            cancelled = db.query(Transformation).filter(Transformation.id == transformation_id).first()
            cancelled_path = None
            if cancelled:
                cancelled_path = Path(cancelled.output_path)
                cancelled.status = "cancelled"
                cancelled.error_message = None
                dataset = db.query(Dataset).filter(Dataset.id == cancelled.dataset_id).first()
                if dataset and dataset.version == cancelled.expected_version:
                    dataset.status = "ready"
            db.commit()
            if cancelled_path:
                storage.delete(cancelled_path)
            return {"status": "cancelled", "transformation_id": transformation_id}
        if _retry_exhausted(self):
            transitioned = JobService.fail(
                db,
                task_id,
                str(exc),
                attempt_token=attempt_token,
            )
        else:
            transitioned = JobService.retry(db, task_id, str(exc), attempt_token=attempt_token)
            if not transitioned:
                db.rollback()
                return {"status": "superseded", "transformation_id": transformation_id}
            retryable = db.query(Transformation).filter(Transformation.id == transformation_id).first()
            if retryable:
                retryable.status = "pending"
                retryable.error_message = str(exc)[:2_000]
                dataset = db.query(Dataset).filter(Dataset.id == retryable.dataset_id).first()
                if dataset and dataset.version == retryable.expected_version:
                    dataset.status = "transforming"
        if not transitioned:
            db.rollback()
            return {"status": "superseded", "transformation_id": transformation_id}
        db.commit()
        raise
    except Exception as exc:
        db.rollback()
        if completed is not None:
            outcome = _recover_transformation_finalization(
                task_id,
                attempt_token,
                transformation_id,
                str(exc),
            )
            if outcome == "completed" and result is not None:
                CacheService.delete(f"dataset:{completed_dataset_id}:profile")
                return result
            if outcome in {"cancelled", "superseded"}:
                return {"status": outcome, "transformation_id": transformation_id}
            raise
        transitioned = JobService.fail(
            db,
            task_id,
            str(exc),
            attempt_token=attempt_token,
        )
        if not transitioned:
            db.rollback()
            return {"status": "superseded", "transformation_id": transformation_id}
        db.commit()
        raise
    finally:
        db.close()
