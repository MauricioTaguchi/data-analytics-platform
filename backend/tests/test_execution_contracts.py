"""Real child-process limits and version-pinned admission across deployments."""

from dataclasses import replace
import sqlite3

import pandas as pd
import pytest

from app.api.v1.routes import datasets as routes
from app.db.session import SessionLocal
from app.models.dataset_event import DatasetEvent
from app.models.transformation import Transformation
from app.services import operations
from app.services.dataset_service import DatasetService
from app.services.engines import EngineResourceLimitError, PandasEngineAdapter, ResourcePolicy, SnapshotRef, TransformationPlan
from app.services.engines import runner
from app.services.outbox_service import OutboxService
from tests.conftest import wait_for_job
from tests.test_operation_admission import upload_dataset


def _policy(**overrides):
    return replace(ResourcePolicy(max_output_bytes=1024 * 1024, quota_remaining_bytes=1024 * 1024,
                                  checkpoint_seconds=0.01), **overrides)


def _source(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("value,name\n1,A\n1,A\n2,B\n")
    return SnapshotRef(source)


def _capture_processes(monkeypatch):
    processes = []
    real_popen = runner.subprocess.Popen

    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(runner.subprocess, "Popen", capture)
    return processes


def test_default_pandas_adapter_executes_in_a_fresh_process(tmp_path, monkeypatch):
    source = _source(tmp_path)
    children = _capture_processes(monkeypatch)
    output = tmp_path / "output.parquet"
    artifact = PandasEngineAdapter().materialize(
        source, TransformationPlan("drop_duplicates", {}), output, _policy(),
    )
    assert len(children) == 1 and children[0].poll() == 0
    assert artifact.rows == 2
    assert artifact.semantics == "pandas:drop_duplicates@1.0.0"
    assert pd.read_parquet(output).value.tolist() == [1, 2]
    assert not runner.spill_directory(output).exists()


@pytest.mark.parametrize("limit", ["deadline", "memory", "cancel"])
def test_pandas_supervisor_terminates_and_joins_work_before_returning(tmp_path, monkeypatch, limit):
    source = _source(tmp_path)
    children = _capture_processes(monkeypatch)
    output = tmp_path / "output.parquet"
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if limit == "cancel" and calls > 1:
            raise RuntimeError("lease lost")

    policy = _policy()
    if limit == "deadline":
        policy = replace(policy, timeout_seconds=0.001)
    elif limit == "memory":
        policy = replace(policy, memory_limit_bytes=1)
    error = RuntimeError if limit == "cancel" else EngineResourceLimitError
    with pytest.raises(error, match={"deadline": "deadline", "memory": "memory", "cancel": "lease lost"}[limit]):
        PandasEngineAdapter().materialize(source, TransformationPlan("drop_duplicates", {}), output, policy, checkpoint)
    assert len(children) == 1 and children[0].poll() is not None
    assert not runner.spill_directory(output).exists()
    assert source.path.read_text() == "value,name\n1,A\n1,A\n2,B\n"


def test_unknown_persisted_version_is_rejected_without_publishing(tmp_path):
    output = tmp_path / "output.parquet"
    with pytest.raises(ValueError, match="drop_duplicates@99.0.0"):
        PandasEngineAdapter().materialize(
            _source(tmp_path), TransformationPlan("drop_duplicates", {}, "99.0.0"), output, _policy(),
        )
    assert not output.exists()


def test_profile_and_inspection_use_the_same_supervised_boundary(tmp_path, monkeypatch):
    children = _capture_processes(monkeypatch)
    source = _source(tmp_path)
    assert runner.run_inspection(source, _policy())["rows"] == 3
    assert runner.run_profile(source, _policy())["summary"]["duplicate_rows"] == 1
    assert len(children) == 2 and all(child.poll() == 0 for child in children)
    with pytest.raises(EngineResourceLimitError, match="memory"):
        runner.run_profile(source, _policy(memory_limit_bytes=1))


def _upgrade_default(monkeypatch):
    registry = operations.OperationRegistry()
    for definition in operations.OPERATIONS.values():
        registry.register(definition)
    version_two = replace(
        registry.resolve("drop_duplicates"), semantic_version="2.0.0",
        pandas_handler=lambda frame, _: frame.iloc[0:0].copy(),
    )
    registry.register(version_two)
    monkeypatch.setattr(operations, "OPERATION_REGISTRY", registry)
    return registry


def test_pending_transformation_and_audit_keep_v1_after_v2_becomes_current(client, auth_headers, monkeypatch):
    dataset_id = upload_dataset(client, auth_headers)
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    request = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1}
    headers = {**auth_headers, "Idempotency-Key": "pre-upgrade-operation"}
    admitted = client.post(f"/api/v1/datasets/{dataset_id}/transform", headers=headers, json=request)
    assert admitted.status_code == 202
    task_id = admitted.json()["task_id"]
    with SessionLocal() as db:
        assert db.query(Transformation).filter_by(task_id=task_id).one().operation_version == "1.0.0"
    registry = _upgrade_default(monkeypatch)
    assert registry.resolve("drop_duplicates").semantic_version == "2.0.0"
    assert DatasetService.apply_operation(pd.DataFrame({"value": [1, 1, 2]}), "drop_duplicates", {}).empty
    replay = client.post(f"/api/v1/datasets/{dataset_id}/transform", headers=headers, json=request)
    assert replay.status_code == 202 and replay.json()["task_id"] == task_id
    with SessionLocal() as db:
        assert OutboxService.dispatch_task(db, task_id)
    result = wait_for_job(client, auth_headers, task_id)
    assert result["after_rows"] == 2
    with SessionLocal() as db:
        transformation = db.query(Transformation).filter_by(task_id=task_id).one()
        event = db.query(DatasetEvent).filter_by(task_id=task_id).one()
        assert transformation.operation_version == event.operation_version == "1.0.0"
        assert event.result_json["operation_version"] == "1.0.0"
        assert pd.read_parquet(transformation.output_path).value.tolist() == [1, 2]


def test_pending_preview_retains_its_admitted_version(client, auth_headers, monkeypatch):
    dataset_id = upload_dataset(client, auth_headers)
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    admitted = client.post(f"/api/v1/datasets/{dataset_id}/transform/preview", headers=auth_headers,
                           json={"operation": "drop_duplicates", "parameters": {}, "expected_version": 1})
    assert admitted.status_code == 202
    _upgrade_default(monkeypatch)
    task_id = admitted.json()["task_id"]
    with SessionLocal() as db:
        assert OutboxService.dispatch_task(db, task_id)
    assert wait_for_job(client, auth_headers, task_id)["after"]["rows"] == 2


def test_registry_refuses_to_replace_released_semantics():
    registry = operations.OperationRegistry()
    definition = operations.OPERATIONS["drop_duplicates"]
    registry.register(definition)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(replace(definition, pandas_handler=lambda frame, _: frame))


def test_migration_pins_existing_pending_operations_to_v1(tmp_path):
    from tests.test_consistency_migration import _migrate, _populated_previous_revision

    database = _populated_previous_revision(tmp_path)
    before = _migrate(database, "20261013_0012")
    assert before.returncode == 0, before.stderr
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE transformations SET status='pending' WHERE id=2")
    after = _migrate(database, "20261013_0013")
    assert after.returncode == 0, after.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT operation_version FROM transformations WHERE id=2").fetchone() == ("1.0.0",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_idempotent_replay_accepts_the_released_wire_schema_after_upgrade(client, auth_headers, monkeypatch):
    dataset_id = upload_dataset(client, auth_headers)
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    payload = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1}
    headers = {**auth_headers, "Idempotency-Key": "original-contract"}
    first = client.post(f"/api/v1/datasets/{dataset_id}/transform", headers=headers, json=payload)
    assert first.status_code == 202

    class VersionTwoParameters(operations.OperationParameters):
        subset: list[str]

    registry = operations.OperationRegistry()
    for definition in operations.OPERATIONS.values():
        registry.register(definition)
    registry.register(replace(registry.resolve("drop_duplicates"), semantic_version="2.0.0",
                              parameters_model=VersionTwoParameters))
    monkeypatch.setattr(operations, "OPERATION_REGISTRY", registry)
    replay = client.post(f"/api/v1/datasets/{dataset_id}/transform", headers=headers, json=payload)
    assert replay.status_code == 202
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert replay.json()["reused"] is True
    with pytest.raises(ValueError):
        operations.validate_operation("drop_duplicates", {})
