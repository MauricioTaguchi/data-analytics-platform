from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.quality_contract import QualityContract
from app.models.quality_evaluation import QualityEvaluation
from app.services.engines import EngineResourceLimitError, ResourcePolicy, SnapshotRef
from app.services.job_service import JobCancellationRequested, JobLeaseUnavailable, JobService, JobStateConflict
from app.services.quality_service import QualityService
from app.worker import celery_app


@celery_app.task(bind=True, name="datasets.quality_evaluate", max_retries=3, soft_time_limit=240, time_limit=300)
def evaluate_quality_task(self, evaluation_id: int):
    from app.services.engines.runner import run_quality

    task_id, attempt_token = str(self.request.id), uuid4().hex
    db = SessionLocal()
    try:
        evaluation = db.query(QualityEvaluation).filter_by(id=evaluation_id, task_id=task_id).first()
        job = JobService.get(db, task_id)
        if evaluation is None or job is None or (
            job.kind != "quality-evaluation" or job.dataset_id != evaluation.dataset_id or job.owner_id != evaluation.owner_id
        ):
            raise JobStateConflict("Task arguments do not match the durable quality evaluation.")
        owner_id, dataset_id = evaluation.owner_id, evaluation.dataset_id
        contract = db.query(QualityContract).filter_by(
            id=evaluation.contract_id, dataset_id=dataset_id, owner_id=owner_id, version=evaluation.contract_version,
        ).first()
        if contract is None:
            raise JobStateConflict("Quality contract does not match the durable evaluation.")
        rules = contract.rules_json
        source = SnapshotRef(Path(evaluation.snapshot_path))
        expected_digest = evaluation.snapshot_sha256
        JobService.lock_owner(db, owner_id)
        job, acquired = JobService.start(db, task_id, attempt_token=attempt_token, stage="quality-checks", progress=10)
        if not acquired:
            return job.result_json or {"status": job.status.lower(), "evaluation_id": evaluation_id}
        QualityService.owned_dataset(db, dataset_id, owner_id)
        evaluation.status = "running"
        db.commit()

        def checkpoint():
            JobService.ensure_active(db, task_id, attempt_token=attempt_token)
            db.commit()

        policy = ResourcePolicy(
            max_output_bytes=settings.MAX_JOB_RESULT_SIZE_MB * 1024 * 1024,
            quota_remaining_bytes=settings.MAX_JOB_RESULT_SIZE_MB * 1024 * 1024,
            threads=settings.TRANSFORMATION_ENGINE_THREADS,
            memory_limit_bytes=settings.TRANSFORMATION_ENGINE_MEMORY_MB * 1024 * 1024,
            max_spill_bytes=settings.TRANSFORMATION_ENGINE_SPILL_MB * 1024 * 1024,
            timeout_seconds=settings.TRANSFORMATION_ENGINE_TIMEOUT_SECONDS,
            max_rows=settings.MAX_DATASET_ROWS, max_columns=settings.MAX_DATASET_COLUMNS,
            max_column_name_chars=settings.MAX_DATASET_COLUMN_NAME_CHARS,
        )
        evidence = run_quality(source, rules, policy, checkpoint)
        if expected_digest is not None and evidence["snapshot_sha256"] != expected_digest:
            raise ValueError("Snapshot failed integrity verification.")
        checkpoint()
        # Fence completion and evidence in one transaction. A stale attempt may
        # neither publish evidence nor overwrite another attempt's final result.
        result = {"status": "completed", "evaluation_id": evaluation_id, "passed": evidence["failed_count"] == 0}
        if not JobService.succeed(db, task_id, result, attempt_token=attempt_token):
            db.rollback()
            return {"status": "superseded", "evaluation_id": evaluation_id}
        evaluation = db.query(QualityEvaluation).filter_by(id=evaluation_id).one()
        evaluation.status = "completed"
        evaluation.passed = result["passed"]
        evaluation.snapshot_sha256 = evidence["snapshot_sha256"]
        evaluation.result_json = {**evidence, "evaluator_version": "1.0.0"}
        evaluation.error_message = None
        evaluation.completed_at = datetime.now(timezone.utc)
        db.commit()
        return result
    except JobLeaseUnavailable as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=exc.retry_after_seconds, max_retries=10) from exc
    except JobStateConflict:
        db.rollback()
        return {"status": "superseded", "evaluation_id": evaluation_id}
    except JobCancellationRequested:
        db.rollback()
        if JobService.cancel(db, task_id, attempt_token=attempt_token, enforce_attempt=True):
            evaluation = db.query(QualityEvaluation).filter_by(id=evaluation_id, task_id=task_id).first()
            if evaluation:
                evaluation.status = "cancelled"
                evaluation.completed_at = datetime.now(timezone.utc)
            db.commit()
        else:
            db.rollback()
        return {"status": "cancelled", "evaluation_id": evaluation_id}
    except Exception as exc:
        db.rollback()
        # Parsers may include cell values in exception text. Persist only bounded
        # generic failure descriptions, never raw samples or parser exceptions.
        message = ("Quality evaluation exceeded an execution limit." if isinstance(exc, EngineResourceLimitError)
                   else "Quality evaluation could not finish. Verify snapshot integrity and rule compatibility.")
        if JobService.fail(db, task_id, message, attempt_token=attempt_token):
            evaluation = db.query(QualityEvaluation).filter_by(id=evaluation_id, task_id=task_id).first()
            if evaluation:
                evaluation.status = "error"
                evaluation.error_message = message
                evaluation.completed_at = datetime.now(timezone.utc)
            db.commit()
        else:
            db.rollback()
        return {"status": "error", "evaluation_id": evaluation_id}
    finally:
        db.close()
