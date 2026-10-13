import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest
from pydantic import ValidationError

from app.api.v1.routes import quality as routes
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.quality_contract import QualityContract
from app.models.quality_evaluation import QualityEvaluation
from app.models.task_outbox import TaskOutbox
from app.schemas.quality import QualityContractCreate
from app.services.outbox_service import OutboxService
from app.services.job_service import JobService
from app.services.quality_service import evaluate_quality
from tests.conftest import wait_for_job


def upload(client, headers):
    project = client.post("/api/v1/projects", headers=headers, json={"name": "Contract evaluation"}).json()
    result = client.post(
        f"/api/v1/datasets/project/{project['id']}", headers=headers,
        files={"file": ("input.csv", b"id,value,label\n1,4,A\n1,20,B\n2,,A\n", "text/csv")},
    )
    assert result.status_code == 202, result.text
    wait_for_job(client, headers, result.json()["task_id"])
    return result.json()["dataset_id"]


def save_contract(client, headers, dataset_id, rules=None, version=0):
    response = client.post(f"/api/v1/datasets/{dataset_id}/quality/contracts", headers=headers, json={
        "name": "Orders contract", "expected_contract_version": version,
        "rules": rules or [{"id": "required_id", "kind": "not_null", "column": "id"}],
    })
    assert response.status_code == 201, response.text
    return response.json()


def evaluate(client, headers, dataset_id, contract_id, key="evaluate", version=1, task_id=None):
    extra = {"X-Task-ID": task_id} if task_id else {}
    return client.post(
        f"/api/v1/datasets/{dataset_id}/quality/evaluations",
        headers={**headers, **extra, "Idempotency-Key": key},
        json={"contract_id": contract_id, "expected_version": version},
    )


def test_five_rule_kinds_produce_bounded_counts_and_no_row_samples(tmp_path):
    source = tmp_path / "source.parquet"
    frame = pd.DataFrame({"id": [1, 1, 2], "value": [4.0, 20.0, None], "label": ["A", "PRIVATE-SAMPLE", "A"]})
    frame.to_parquet(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = evaluate_quality(source, [
        {"id": "required", "kind": "not_null", "column": "value"},
        {"id": "distinct", "kind": "unique", "column": "id"},
        {"id": "within", "kind": "range", "column": "value", "min": 0, "max": 10},
        {"id": "allowed", "kind": "accepted_values", "column": "label", "values": ["A"]},
        {"id": "shape", "kind": "schema", "expected_schema": {"id": "int64", "value": "float64", "label": "object"}},
    ])
    assert result["row_count"] == 3
    assert (result["passed_count"], result["failed_count"]) == (1, 4)
    assert [rule["violation_count"] for rule in result["rules"]] == [1, 1, 1, 1, 0]
    assert result["rules"][2]["checked_count"] == 2
    assert "PRIVATE-SAMPLE" not in json.dumps(result, allow_nan=False)
    assert result["snapshot_sha256"] == digest == hashlib.sha256(source.read_bytes()).hexdigest()


def test_schema_drift_missing_columns_and_typed_accepted_values(tmp_path):
    source = tmp_path / "source.parquet"
    pd.DataFrame({"flag": [True, False], "extra": [1, 2]}).to_parquet(source)
    result = evaluate_quality(source, [
        {"id": "typed", "kind": "accepted_values", "column": "flag", "values": [1]},
        {"id": "missing", "kind": "not_null", "column": "absent"},
        {"id": "drift", "kind": "schema", "expected_schema": {"flag": "bool"}},
        {"id": "extra_allowed", "kind": "schema", "expected_schema": {"flag": "bool"}, "allow_extra_columns": True},
    ])
    assert [rule["violation_count"] for rule in result["rules"]] == [2, 1, 1, 0]
    assert result["rules"][1]["checked_count"] == 0


def test_decimal_boundaries_and_null_semantics_are_explicit(tmp_path):
    source = tmp_path / "source.parquet"
    pd.DataFrame({"amount": [Decimal("0.10"), Decimal("0.10"), None]}).to_parquet(source)
    result = evaluate_quality(source, [
        {"id": "bounds", "kind": "range", "column": "amount", "min": 0.1, "max": 0.1},
        {"id": "accepted", "kind": "accepted_values", "column": "amount", "values": [0.1, None]},
        {"id": "distinct", "kind": "unique", "column": "amount"},
        {"id": "required", "kind": "not_null", "column": "amount"},
    ])
    assert [rule["violation_count"] for rule in result["rules"]] == [0, 0, 1, 1]
    assert result["rules"][0]["checked_count"] == 2


@pytest.mark.parametrize("values", [
    [["a", "b"], ["a", "c"], ["a", "b"]],
    [{"value": "x" * 600 + "a"}, {"value": "x" * 600 + "b"}, {"value": "x" * 600 + "a"}],
    [b"a", b"b", b"a"],
])
def test_unique_compares_full_nested_and_binary_values(tmp_path, values):
    source = tmp_path / "structured.parquet"
    pd.DataFrame({"value": values}).to_parquet(source)
    result = evaluate_quality(source, [{"id": "unique", "kind": "unique", "column": "value"}])
    assert result["rules"][0]["checked_count"] == 3
    assert result["rules"][0]["violation_count"] == 1
    assert "x" * 100 not in json.dumps(result)


def test_accepted_values_rejects_array_cells_without_scalar_coercion(tmp_path):
    source = tmp_path / "arrays.parquet"
    pd.DataFrame({"value": [["a", "b"], ["a", "c"]]}).to_parquet(source)
    result = evaluate_quality(source, [{"id": "allowed", "kind": "accepted_values", "column": "value", "values": ["a"]}])
    assert result["rules"][0]["violation_count"] == 2


@pytest.mark.parametrize("rules", [
    [],
    [{"id": f"rule{i}", "kind": "not_null", "column": "id"} for i in range(51)],
    [{"id": "a", "kind": "range", "column": "id"}],
    [{"id": "a", "kind": "range", "column": "id", "min": 9, "max": 2}],
    [{"id": "a", "kind": "range", "column": "id", "min": float("inf")}],
    [{"id": "a", "kind": "not_null", "column": "id", "ignored": True}],
    [{"id": "a", "kind": "accepted_values", "column": "id", "values": [{"nested": 1}]}],
    [{"id": "same", "kind": "unique", "column": "id"}] * 2,
])
def test_contract_rejects_unbounded_or_ambiguous_rules(rules):
    with pytest.raises(ValidationError):
        QualityContractCreate(name="Invalid", rules=rules)


def test_versioned_contract_and_async_evidence_survive_job_retention(client, auth_headers):
    dataset_id = upload(client, auth_headers)
    first = save_contract(client, auth_headers, dataset_id)
    second = save_contract(client, auth_headers, dataset_id, version=1, rules=[{"id": "unique", "kind": "unique", "column": "id"}])
    assert (first["version"], second["version"]) == (1, 2)
    stale = client.post(f"/api/v1/datasets/{dataset_id}/quality/contracts", headers=auth_headers, json={
        "name": "Stale", "rules": first["rules"], "expected_contract_version": 1,
    })
    assert stale.status_code == 409
    task_id = str(uuid4())
    admitted = evaluate(client, auth_headers, dataset_id, second["id"], task_id=task_id)
    assert admitted.status_code == 202, admitted.text
    assert admitted.json()["task_id"] == task_id
    job = wait_for_job(client, auth_headers, task_id)
    assert job["passed"] is False
    evidence = client.get(f"/api/v1/datasets/{dataset_id}/quality/evaluations", headers=auth_headers).json()[0]
    assert evidence["status"] == "completed"
    assert evidence["passed"] is False
    assert evidence["contract_version"] == 2
    assert evidence["dataset_version"] == 1
    assert len(evidence["snapshot_sha256"]) == 64
    assert "snapshot_path" not in evidence
    assert evidence["result"]["rules"][0]["violation_count"] == 1
    assert evidence["stale"] is False
    with SessionLocal() as db:
        db.query(TaskOutbox).filter_by(task_id=task_id).delete()
        db.query(JobRecord).filter_by(task_id=task_id).delete()
        db.commit()
    replay = evaluate(client, auth_headers, dataset_id, second["id"])
    assert replay.status_code == 202
    assert replay.json()["reused"] is True
    assert replay.json()["evaluation_id"] == evidence["id"]
    retained = client.get(f"/api/v1/datasets/{dataset_id}/quality/evaluations/{evidence['id']}", headers=auth_headers)
    assert retained.json()["result"] == evidence["result"]


def test_evaluation_admission_is_atomic_idempotent_and_tenant_scoped(client, auth_headers, monkeypatch):
    dataset_id = upload(client, auth_headers)
    contract = save_contract(client, auth_headers, dataset_id)
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    admitted = evaluate(client, auth_headers, dataset_id, contract["id"])
    assert admitted.status_code == 202, admitted.text
    task_id = admitted.json()["task_id"]
    replay = evaluate(client, auth_headers, dataset_id, contract["id"])
    assert replay.json()["task_id"] == task_id
    assert replay.json()["reused"] is True
    conflict = evaluate(client, auth_headers, dataset_id, contract["id"], version=2)
    assert conflict.status_code == 409
    with SessionLocal() as db:
        assert db.query(QualityEvaluation).count() == 1
        assert db.query(TaskOutbox).filter_by(task_id=task_id).one().payload_json["args"] == [admitted.json()["evaluation_id"]]
        assert db.query(JobRecord).filter_by(task_id=task_id).one().kind == "quality-evaluation"
        assert db.query(Dataset).filter_by(id=dataset_id).one().status == "ready"
    stranger = client.post("/api/v1/auth/register", json={"name": "Other", "email": "quality-other@example.com", "password": "password12345"}).json()
    headers = {"Authorization": f"Bearer {stranger['access_token']}"}
    for suffix in ("contracts", "evaluations", f"evaluations/{admitted.json()['evaluation_id']}"):
        assert client.get(f"/api/v1/datasets/{dataset_id}/quality/{suffix}", headers=headers).status_code == 404
    assert evaluate(client, headers, dataset_id, contract["id"], key="other").status_code == 404
    with SessionLocal() as db:
        assert OutboxService.dispatch_task(db, task_id)
    assert wait_for_job(client, auth_headers, task_id)["passed"] is True


@pytest.mark.parametrize("termination", ["cancel_pending", "cancel_running", "worker_failure", "recovery_failure"])
def test_terminal_evaluation_survives_job_retention(client, auth_headers, monkeypatch, termination):
    dataset_id = upload(client, auth_headers)
    contract = save_contract(client, auth_headers, dataset_id)
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    admitted = evaluate(client, auth_headers, dataset_id, contract["id"]).json()
    task_id = admitted["task_id"]
    if termination == "cancel_pending":
        assert client.delete(f"/api/v1/datasets/jobs/{task_id}", headers=auth_headers).status_code == 204
    else:
        with SessionLocal() as db:
            job, acquired = JobService.start(db, task_id, attempt_token="quality-attempt")
            assert acquired
            evaluation = db.query(QualityEvaluation).filter_by(task_id=task_id).one()
            evaluation.status = "running"
            db.commit()
            if termination == "cancel_running":
                assert JobService.cancel(db, task_id, attempt_token="quality-attempt")
            elif termination == "worker_failure":
                assert JobService.fail(db, task_id, "PRIVATE-SAMPLE", attempt_token="quality-attempt")
            else:
                job = JobService.get(db, task_id)
                job.attempt_count = job.max_attempts
                job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
                db.commit()
                assert OutboxService.reconcile(db)["failed"] == [task_id]
            db.commit()
    expected_status = "cancelled" if termination.startswith("cancel") else "error"
    with SessionLocal() as db:
        evidence = db.query(QualityEvaluation).filter_by(task_id=task_id).one()
        assert evidence.status == expected_status
        assert evidence.completed_at is not None
        assert evidence.passed is None and evidence.result_json is None
        db.query(TaskOutbox).filter_by(task_id=task_id).delete()
        db.query(JobRecord).filter_by(task_id=task_id).delete()
        db.commit()
    retained = client.get(f"/api/v1/datasets/{dataset_id}/quality/evaluations/{admitted['evaluation_id']}", headers=auth_headers)
    assert retained.json()["status"] == expected_status
    assert retained.json()["completed_at"] is not None
    assert "PRIVATE-SAMPLE" not in json.dumps(retained.json())


def test_evaluation_retains_admitted_snapshot_when_head_changes(client, auth_headers, monkeypatch):
    dataset_id = upload(client, auth_headers)
    contract = save_contract(client, auth_headers, dataset_id)
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    admitted = evaluate(client, auth_headers, dataset_id, contract["id"]).json()
    transformed = client.post(f"/api/v1/datasets/{dataset_id}/transform", headers={**auth_headers, "Idempotency-Key": "change-head"}, json={
        "operation": "drop_columns", "parameters": {"columns": ["id"]}, "expected_version": 1,
    })
    assert transformed.status_code == 202, transformed.text
    wait_for_job(client, auth_headers, transformed.json()["task_id"])
    with SessionLocal() as db:
        assert OutboxService.dispatch_task(db, admitted["task_id"])
    assert wait_for_job(client, auth_headers, admitted["task_id"])["passed"] is True
    evidence = client.get(f"/api/v1/datasets/{dataset_id}/quality/evaluations", headers=auth_headers).json()[0]
    assert evidence["stale"] is True
    assert evidence["dataset_version"] == 1


def test_corrupt_snapshot_does_not_publish_quality_pass(client, auth_headers, monkeypatch):
    dataset_id = upload(client, auth_headers)
    contract = save_contract(client, auth_headers, dataset_id)
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    admitted = evaluate(client, auth_headers, dataset_id, contract["id"]).json()
    with SessionLocal() as db:
        evaluation = db.query(QualityEvaluation).one()
        Path(evaluation.snapshot_path).write_bytes(b"id,value,label\n999,9,SECRET-CELL\n")
        assert OutboxService.dispatch_task(db, admitted["task_id"])
    evidence = client.get(f"/api/v1/datasets/{dataset_id}/quality/evaluations", headers=auth_headers).json()[0]
    assert evidence["status"] == "error"
    assert evidence["passed"] is None
    assert evidence["result"] is None
    assert "SECRET-CELL" not in json.dumps(evidence)


def test_contract_rows_are_immutable(client, auth_headers):
    dataset_id = upload(client, auth_headers)
    save_contract(client, auth_headers, dataset_id)
    with SessionLocal() as db:
        contract = db.query(QualityContract).one()
        contract.name = "Modified historical contract"
        with pytest.raises(ValueError, match="immutable"):
            db.commit()
        db.rollback()


def test_completed_evidence_cannot_be_rewritten(client, auth_headers):
    dataset_id = upload(client, auth_headers)
    contract = save_contract(client, auth_headers, dataset_id)
    admitted = evaluate(client, auth_headers, dataset_id, contract["id"])
    assert admitted.status_code == 202
    wait_for_job(client, auth_headers, admitted.json()["task_id"])
    with SessionLocal() as db:
        evidence = db.query(QualityEvaluation).one()
        evidence.passed = False
        with pytest.raises(ValueError, match="immutable"):
            db.commit()
        db.rollback()
