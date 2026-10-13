"""Quality evidence through real broker, worker and API processes."""

from uuid import uuid4


def test_quality_evidence_survives_process_restart_and_duplicate_delivery(runtime):
    dataset_id, headers, _ = runtime.create_dataset()
    contract = runtime.request(
        "POST", f"/datasets/{dataset_id}/quality/contracts", headers=headers, expected=201,
        json={"name": "Unique names", "rules": [{"id": "unique_names", "kind": "unique", "column": "name"}]},
    ).json()
    admitted = runtime.request(
        "POST", f"/datasets/{dataset_id}/quality/evaluations", expected=202,
        headers={**headers, "Idempotency-Key": uuid4().hex},
        json={"contract_id": contract["id"], "expected_version": 1},
    ).json()
    job = runtime.wait_job(admitted["task_id"], headers)
    assert job["result"]["passed"] is False
    path = f"/datasets/{dataset_id}/quality/evaluations/{admitted['evaluation_id']}"
    evidence = runtime.request("GET", path, headers=headers).json()
    assert evidence["status"] == "completed"
    assert evidence["result"]["rules"][0]["violation_count"] == 1
    assert evidence["dataset_version"] == 1
    assert len(evidence["snapshot_sha256"]) == 64
    runtime.publisher.send_task("datasets.quality_evaluate", args=[admitted["evaluation_id"]], task_id=admitted["task_id"])
    runtime.barrier()
    runtime.stop("api")
    runtime.start_api()
    assert runtime.request("GET", path, headers=headers).json() == evidence
    assert runtime.rows("SELECT attempt_count FROM job_records WHERE task_id=:id", id=admitted["task_id"])[0]["attempt_count"] == 1
