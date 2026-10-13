from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.dataset_event import DatasetEvent
from app.models.transformation import Transformation
from app.services.engines.contracts import MaterializedArtifact


class DatasetAuditService:
    """Append receipts in the caller's head-update transaction; never commit here."""

    UNDO_VERSION = "1.0.0"

    @staticmethod
    def fingerprint(*, kind: str, dataset_id: int, expected_version: int, **parameters) -> str:
        payload = {"kind": kind, "dataset_id": dataset_id, "expected_version": expected_version, **parameters}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def replay(
        db: Session, *, dataset_id: int, actor_id: int, kind: str,
        idempotency_key: str | None, request_fingerprint: str,
    ) -> dict | None:
        if idempotency_key is None:
            return None
        event = db.query(DatasetEvent).filter_by(
            dataset_id=dataset_id, actor_id=actor_id, kind=kind, idempotency_key=idempotency_key,
        ).first()
        if event is None:
            return None
        if event.request_fingerprint != request_fingerprint:
            raise ValueError("Idempotency-Key has already been used with a different undo request.")
        return deepcopy(event.result_json)

    @staticmethod
    def transformation_result(transformation: Transformation) -> dict:
        return {
            "status": "completed",
            "transformation_id": transformation.id,
            "dataset_id": transformation.dataset_id,
            "before_rows": transformation.before_rows,
            "after_rows": transformation.after_rows,
            "before_columns": transformation.before_columns,
            "after_columns": transformation.after_columns,
            "progress": 100,
        }

    @classmethod
    def record_transformation(
        cls, db: Session, transformation: Transformation, *, artifact: MaterializedArtifact,
    ) -> DatasetEvent:
        from app.services.operations import operation_definition

        receipt = cls.transformation_result(transformation)
        receipt.update(
            result_version=transformation.expected_version + 1,
            engine_name=transformation.engine_name,
            snapshot={
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "semantics": artifact.semantics,
            },
        )
        record = DatasetEvent(
            id=uuid4().hex,
            dataset_id=transformation.dataset_id,
            actor_id=transformation.user_id,
            transformation_id=transformation.id,
            kind="transformation",
            idempotency_key=transformation.idempotency_key,
            request_fingerprint=cls.fingerprint(
                kind="transformation", dataset_id=transformation.dataset_id,
                expected_version=transformation.expected_version,
                operation=transformation.operation, parameters=transformation.parameters,
                engine_name=transformation.engine_name,
            ),
            task_id=transformation.task_id,
            previous_version=transformation.expected_version,
            result_version=transformation.expected_version + 1,
            input_path=transformation.input_path,
            output_path=transformation.output_path,
            operation_version=operation_definition(transformation.operation).semantic_version,
            result_json=receipt,
        )
        db.add(record)
        db.flush()
        return record

    @classmethod
    def record_undo(
        cls, db: Session, transformation: Transformation, *, actor_id: int,
        previous_version: int, idempotency_key: str | None, request_fingerprint: str,
    ) -> dict:
        event_id = uuid4().hex
        # Capture a response, not an ORM reference. A later transformation/undo
        # must never change the result returned for this operation's key.
        response = {
            "id": transformation.id,
            "operation": transformation.operation,
            "parameters": deepcopy(transformation.parameters),
            "status": "undone",
            "task_id": transformation.task_id,
            "expected_version": transformation.expected_version,
            "before_rows": transformation.before_rows,
            "after_rows": transformation.after_rows,
            "before_columns": transformation.before_columns,
            "after_columns": transformation.after_columns,
            "created_at": transformation.created_at.isoformat() if transformation.created_at else None,
            "undone_at": transformation.undone_at.isoformat() if transformation.undone_at else None,
            "event_id": event_id,
            "previous_version": previous_version,
            "result_version": previous_version + 1,
        }
        db.add(DatasetEvent(
            id=event_id,
            dataset_id=transformation.dataset_id,
            actor_id=actor_id,
            transformation_id=transformation.id,
            kind="undo",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            previous_version=previous_version,
            result_version=previous_version + 1,
            input_path=transformation.output_path,
            output_path=transformation.input_path,
            operation_version=cls.UNDO_VERSION,
            result_json=deepcopy(response),
        ))
        db.flush()
        return response

    @staticmethod
    def archived_job(db: Session, task_id: str, actor_id: int) -> dict | None:
        event = db.query(DatasetEvent).filter_by(
            task_id=task_id, actor_id=actor_id, kind="transformation",
        ).first()
        if event is not None:
            return {
                "task_id": task_id, "status": "SUCCESS", "progress": 100,
                "stage": "completed", "error_message": None,
                "result": deepcopy(event.result_json),
            }
        # Existing installations have no fabricated audit backfill. Preserve
        # their terminal lookup using known facts, without guessing a revision.
        transformation = db.query(Transformation).filter_by(task_id=task_id, user_id=actor_id).first()
        if transformation is None:
            return None
        if transformation.status in {"completed", "undone"}:
            return {
                "task_id": task_id, "status": "SUCCESS", "progress": 100,
                "stage": "completed", "error_message": None,
                "result": DatasetAuditService.transformation_result(transformation),
            }
        if transformation.status in {"failed", "cancelled"}:
            return {
                "task_id": task_id,
                "status": "FAILURE" if transformation.status == "failed" else "CANCELLED",
                "progress": 0, "stage": transformation.status,
                "error_message": transformation.error_message, "result": None,
            }
        return None
