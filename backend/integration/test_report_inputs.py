"""Report provenance and immutable source identity through migrated PostgreSQL."""
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_report_provenance_survives_revision_change_and_duplicate_delivery(runtime):
    dataset_id, headers, _ = runtime.create_dataset()
    dataset = runtime.request("GET", f"/datasets/{dataset_id}", headers=headers).json()
    project_id, task_id = dataset["project_id"], str(uuid4())
    route = f"/reports/project/{project_id}/dataset/{dataset_id}"
    admitted = runtime.request("POST", route, expected=202, headers={**headers, "X-Task-ID": task_id}).json()
    runtime.wait_job(task_id, headers)
    report_id = admitted["report_id"]
    path = f"/reports/{report_id}"
    report = runtime.request("GET", path, headers=headers).json()
    assert report["status"] == "completed" and report["dataset_version"] == 1
    assert report["snapshot_version"] == 1 and len(report["snapshot_sha256"]) == 64
    before = runtime.http.get(report["download_url"], headers=headers)
    assert before.status_code == 200 and before.content.startswith(b"%PDF")

    transformation = runtime.transform(dataset_id, headers)
    runtime.wait_job(transformation["task_id"], headers)
    replay = runtime.request("POST", route, expected=202, headers={**headers, "X-Task-ID": task_id}).json()
    assert replay["reused"] is True and replay["report_id"] == report_id
    assert replay["dataset_version"] == 1
    runtime.publisher.send_task("reports.generate", args=[report_id], task_id=task_id)
    runtime.barrier()
    assert runtime.request("GET", path, headers=headers).json() == report
    assert runtime.http.get(report["download_url"], headers=headers).content == before.content
    assert runtime.rows("SELECT attempt_count FROM job_records WHERE task_id=:id", id=task_id)[0]["attempt_count"] == 1

    for mutation in ("dataset_version=99", "snapshot_sha256=repeat('0',64)", "snapshot_path='another.csv'", "snapshot_id=NULL"):
        with pytest.raises(DBAPIError, match="Report input identity is immutable"):
            with runtime.database.begin() as connection:
                connection.execute(text(f"UPDATE reports SET {mutation} WHERE id=:id"), {"id": report_id})
