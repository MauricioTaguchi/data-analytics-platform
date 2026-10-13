"""Versioned contracts and snapshot-bound quality evidence without row samples."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from numbers import Real
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.dataset import Dataset
from app.models.project import Project
from app.models.quality_contract import QualityContract
from app.models.quality_evaluation import QualityEvaluation
from app.schemas.quality import QualityContractCreate, QualityEvaluationCreate
from app.services.dataset_service import DatasetService
from app.services.job_service import JobService
from app.services.outbox_service import OutboxService


def evaluate_quality(path: Path, rules: list[dict]) -> dict:
    """Runs only inside the resource-supervised child in production.

    Nullability excludes empty text. Uniqueness ignores nulls and counts duplicate
    occurrences after the first. Ranges ignore nulls and require real finite
    numbers, with inclusive bounds. Accepted values compare typed scalars, with
    null allowed only when explicitly listed. Schema matches exact dtype names.
    """
    import numpy as np
    import pandas as pd
    from app.services.snapshot_service import SnapshotService

    contract = QualityContractCreate.model_validate({"name": "Evaluation", "rules": rules})
    fingerprint = SnapshotService.digest(path)
    frame = DatasetService.read_dataframe(path)
    row_count = len(frame)
    results = []

    def is_null(value):
        return value is None or (pd.api.types.is_scalar(value) and bool(pd.isna(value)))

    def scalar_key(value):
        if is_null(value):
            return ("null", None)
        if isinstance(value, (bool, np.bool_)):
            return ("boolean", bool(value))
        if isinstance(value, (Real, Decimal)):
            return ("number", Decimal(str(value)))
        if isinstance(value, str):
            return ("text", value)
        return ("unsupported", None)

    def uniqueness_key(value):
        """Compare full typed values; presentation truncation must never affect evidence."""
        key = scalar_key(value)
        if key[0] != "unsupported":
            return key
        if isinstance(value, (bytes, bytearray, memoryview)):
            return ("binary", bytes(value))
        if isinstance(value, (list, tuple, np.ndarray)):
            return ("sequence", tuple(uniqueness_key(item) for item in value))
        if isinstance(value, dict):
            return ("mapping", frozenset((uniqueness_key(name), uniqueness_key(item)) for name, item in value.items()))
        if isinstance(value, np.datetime64):
            return ("datetime", pd.Timestamp(value))
        if isinstance(value, np.timedelta64):
            return ("timedelta", pd.Timedelta(value))
        if isinstance(value, datetime):
            return ("datetime", value)
        if isinstance(value, (date, time, timedelta)):
            return (type(value).__name__, value)
        raise ValueError("Uniqueness is unsupported for a value type in this column.")

    for rule_model in contract.rules:
        rule = rule_model.model_dump()
        kind = rule["kind"]
        column = rule.get("column")
        checked = row_count
        violations = 0
        message = "Rule passed."
        if kind == "schema":
            expected = rule["expected_schema"]
            actual = {str(name): str(dtype) for name, dtype in frame.dtypes.items()}
            checked = len(expected) + (0 if rule["allow_extra_columns"] else len(set(actual) - set(expected)))
            violations = sum(actual.get(name) != dtype for name, dtype in expected.items())
            if not rule["allow_extra_columns"]:
                violations += len(set(actual) - set(expected))
            message = "Schema matches the contract." if not violations else "Schema columns or dtypes differ from the contract."
        elif column not in frame.columns:
            checked = 0
            violations = 1
            message = "Required column is missing."
        else:
            series = frame[column]
            if kind == "not_null":
                violations = int(series.isna().sum())
            elif kind == "unique":
                present = series.dropna()
                checked = len(present)
                keys = present.map(uniqueness_key)
                violations = int(keys.duplicated(keep="first").sum())
            elif kind == "range":
                present = series.dropna()
                checked = len(present)
                def outside_range(value, minimum=rule["min"], maximum=rule["max"]):
                    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
                        return True
                    numeric = Decimal(str(value))
                    if not numeric.is_finite():
                        return True
                    return ((minimum is not None and numeric < Decimal(str(minimum)))
                            or (maximum is not None and numeric > Decimal(str(maximum))))
                violations = int(present.map(outside_range).sum())
            elif kind == "accepted_values":
                allowed = {scalar_key(value) for value in rule["values"]}
                violations = int(series.map(lambda value, accepted=allowed: scalar_key(value) not in accepted).sum())
            if violations:
                message = "Values violate the configured rule."
        results.append({
            "rule_id": rule["id"], "kind": kind, "column": column,
            "passed": violations == 0, "checked_count": int(checked),
            "violation_count": int(violations), "message": message,
        })
    fingerprint.assert_unchanged(path)
    after_digest = SnapshotService.digest(path).sha256
    if after_digest != fingerprint.sha256:
        raise ValueError("Source snapshot changed during quality evaluation.")
    passed_count = sum(result["passed"] for result in results)
    return {
        "snapshot_sha256": fingerprint.sha256, "row_count": row_count,
        "rule_count": len(results), "passed_count": passed_count,
        "failed_count": len(results) - passed_count, "rules": results,
    }


class QualityService:
    @staticmethod
    def owned_dataset(db: Session, dataset_id: int, owner_id: int, *, lock: bool = False) -> Dataset:
        query = db.query(Dataset).execution_options(populate_existing=True).join(Project).filter(
            Dataset.id == dataset_id, Project.owner_id == owner_id, Dataset.deleted_at.is_(None),
        )
        if lock:
            query = query.with_for_update(of=Dataset)
        dataset = query.first()
        if dataset is None:
            raise LookupError("Dataset not found.")
        return dataset

    @classmethod
    def create_contract(cls, db: Session, dataset_id: int, owner_id: int, payload: QualityContractCreate):
        JobService.lock_owner(db, owner_id)
        cls.owned_dataset(db, dataset_id, owner_id, lock=True)
        latest = db.query(QualityContract).filter_by(dataset_id=dataset_id).order_by(QualityContract.version.desc()).first()
        version = latest.version if latest else 0
        if version != payload.expected_contract_version:
            raise ValueError("Quality contract version changed. Reload the latest contract before saving.")
        contract = QualityContract(
            dataset_id=dataset_id, owner_id=owner_id, version=version + 1,
            name=payload.name.strip(), rules_json=[rule.model_dump() for rule in payload.rules],
        )
        db.add(contract)
        db.flush()
        return contract

    @classmethod
    def admit_evaluation(
        cls, db: Session, dataset_id: int, owner_id: int,
        payload: QualityEvaluationCreate, idempotency_key: str, *, task_id: str | None = None,
    ) -> tuple[QualityEvaluation, bool]:
        from app.services.snapshot_service import SnapshotService

        JobService.lock_owner(db, owner_id)
        dataset = cls.owned_dataset(db, dataset_id, owner_id, lock=True)
        existing = db.query(QualityEvaluation).filter_by(
            dataset_id=dataset_id, owner_id=owner_id, idempotency_key=idempotency_key,
        ).first()
        if existing:
            if existing.contract_id != payload.contract_id or existing.dataset_version != payload.expected_version:
                raise ValueError("Idempotency-Key has already been used with a different quality evaluation.")
            return existing, False
        contract = db.query(QualityContract).filter_by(
            id=payload.contract_id, dataset_id=dataset_id, owner_id=owner_id,
        ).first()
        if contract is None:
            raise LookupError("Quality contract not found.")
        DatasetService.ensure_ready(dataset)
        if dataset.version != payload.expected_version:
            raise ValueError("Dataset version changed. Refresh before evaluating quality.")
        manifest = SnapshotService.manifest(db, dataset_id, Path(dataset.stored_path))
        if manifest is not None and manifest.availability != "AVAILABLE":
            raise ValueError("The dataset snapshot is unavailable for evaluation.")
        task_id = task_id or str(uuid4())
        job = JobService.create(db, task_id=task_id, owner_id=owner_id, dataset_id=dataset_id, kind="quality-evaluation")
        db.add(job)
        evaluation = QualityEvaluation(
            dataset_id=dataset_id, owner_id=owner_id, contract_id=contract.id,
            contract_version=contract.version, dataset_version=dataset.version,
            snapshot_path=dataset.stored_path, snapshot_sha256=manifest.sha256 if manifest else None,
            task_id=task_id, idempotency_key=idempotency_key, status="pending",
        )
        db.add(evaluation)
        db.flush()
        OutboxService.enqueue(db, task_id=task_id, kind="quality-evaluation", args=[evaluation.id])
        return evaluation, True
