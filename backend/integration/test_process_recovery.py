"""Small production-process journeys; requires PostgreSQL and Redis services."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from sqlalchemy import text

from integration.conftest import eventually


def test_upload_transform_undo_survives_api_restart(runtime):
    dataset_id, headers, original = runtime.create_dataset()
    admitted = runtime.transform(dataset_id, headers)
    runtime.wait_job(admitted["task_id"], headers)
    preview = runtime.request("GET", f"/datasets/{dataset_id}/preview", headers=headers).json()
    assert preview["total_rows"] == 2
    undo = runtime.request(
        "POST", f"/datasets/{dataset_id}/transformations/undo", headers=headers,
        json={"expected_version": 2},
    ).json()
    assert undo["status"] == "undone"
    runtime.stop("api")
    runtime.start_api()
    dataset = runtime.request("GET", f"/datasets/{dataset_id}", headers=headers).json()
    assert dataset["version"] == 3
    assert runtime.request("GET", f"/datasets/{dataset_id}/export", headers=headers).content == original
    history = runtime.request("GET", f"/datasets/{dataset_id}/transformations", headers=headers).json()
    assert len(history) == 1
    assert history[0]["status"] == "undone"
    runtime.barrier()


def test_durable_admission_recovers_after_publisher_network_partition(runtime):
    dataset_id, headers, _ = runtime.create_dataset()
    runtime.stop_schedulers()
    runtime.proxy.stop()
    # Only the publisher-to-Redis link is unavailable. The API must commit its
    # intent before trying the real broker connection and still return 202.
    admitted = runtime.transform(dataset_id, headers)
    outbox = runtime.rows("SELECT status, attempts, last_error FROM task_outbox WHERE task_id=:task_id",
                          task_id=admitted["task_id"])[0]
    assert outbox["status"] == "PENDING"
    assert outbox["attempts"] >= 1
    assert outbox["last_error"]
    job = runtime.rows("SELECT status, attempt_count FROM job_records WHERE task_id=:task_id",
                       task_id=admitted["task_id"])[0]
    assert job["status"] == "PENDING"
    assert job["attempt_count"] == 0
    # Also lose the API process that accepted the request. Independent scheduler
    # replicas must recover exclusively from the committed database outbox.
    runtime.stop("api", kill=True)
    runtime.proxy.start()
    runtime.start_schedulers()
    runtime.start_api()
    runtime.wait_job(admitted["task_id"], headers)
    assert runtime.rows("SELECT version FROM datasets WHERE id=:id", id=dataset_id)[0]["version"] == 2
    assert runtime.rows("SELECT attempt_count FROM job_records WHERE task_id=:id", id=admitted["task_id"])[0]["attempt_count"] == 1


def test_duplicate_broker_deliveries_do_not_publish_again(runtime):
    dataset_id, headers, _ = runtime.create_dataset()
    admitted = runtime.transform(dataset_id, headers)
    runtime.wait_job(admitted["task_id"], headers)
    before = runtime.rows("SELECT version, stored_path FROM datasets WHERE id=:id", id=dataset_id)[0]
    for _ in range(2):
        runtime.publisher.send_task(
            "datasets.transform", args=[admitted["transformation_id"]], task_id=admitted["task_id"],
        )
    # One FIFO queue and one prefork child: the real barrier executes after both
    # duplicates, so this assertion cannot pass merely because they are queued.
    runtime.barrier()
    assert runtime.rows("SELECT version, stored_path FROM datasets WHERE id=:id", id=dataset_id)[0] == before
    job = runtime.rows("SELECT status, attempt_count FROM job_records WHERE task_id=:id", id=admitted["task_id"])[0]
    assert job == {"status": "SUCCESS", "attempt_count": 1}
    artifacts = runtime.rows("SELECT state FROM storage_artifacts WHERE task_id=:id", id=admitted["task_id"])
    assert artifacts == [{"state": "LIVE"}]


def test_killed_worker_after_rename_recovers_without_publishing_abandoned_output(runtime):
    dataset_id, headers, original = runtime.create_dataset()
    runtime.stop("worker")
    marker = runtime.directory / "after-rename.json"
    runtime.start_worker(marker=marker)
    # Dispatch acknowledgement may wait on the worker's final row lock. Submit
    # concurrently so killing at the marker does not depend on HTTP timing.
    with ThreadPoolExecutor(max_workers=1) as executor:
        admission = executor.submit(runtime.transform, dataset_id, headers)
        eventually(marker.exists, description="worker's completed filesystem rename")
        abandoned_path = Path(json.loads(marker.read_text())["final_path"])
        assert abandoned_path.exists()
        runtime.stop("worker", kill=True)
        admitted = admission.result(timeout=25)
    # SIGKILL closed the database connection; its uncommitted publication rolls
    # back. These observations happen before advancing the abandoned lease.
    dataset = runtime.rows("SELECT version, stored_path FROM datasets WHERE id=:id", id=dataset_id)[0]
    assert dataset["version"] == 1
    assert Path(dataset["stored_path"]).read_bytes() == original
    assert runtime.rows("SELECT state FROM storage_artifacts WHERE task_id=:id", id=admitted["task_id"]) == [{"state": "RESERVED"}]
    # Advance only durable expiry timestamps after actual process death, avoiding
    # a six-minute wait. No state transition/retry/publication is performed here.
    with runtime.database.begin() as connection:
        connection.execute(text("UPDATE job_records SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=:id"),
                           {"id": admitted["task_id"]})
        connection.execute(text("UPDATE storage_artifacts SET expires_at=clock_timestamp()-interval '1 second' WHERE task_id=:id"),
                           {"id": admitted["task_id"]})
    runtime.start_worker()
    runtime.wait_job(admitted["task_id"], headers)

    def abandoned_output_collected():
        artifacts = runtime.rows(
            "SELECT state FROM storage_artifacts WHERE task_id=:id ORDER BY state",
            id=admitted["task_id"],
        )
        return not abandoned_path.exists() and artifacts == [{"state": "DELETED"}, {"state": "LIVE"}]

    eventually(abandoned_output_collected, description="abandoned output collection and durable tombstone")
    dataset = runtime.rows("SELECT version, stored_path FROM datasets WHERE id=:id", id=dataset_id)[0]
    assert dataset["version"] == 2
    assert dataset["stored_path"] != str(abandoned_path)
    assert Path(dataset["stored_path"]).exists()
    job = runtime.rows("SELECT status, attempt_count FROM job_records WHERE task_id=:id", id=admitted["task_id"])[0]
    assert job == {"status": "SUCCESS", "attempt_count": 2}
    artifacts = runtime.rows("SELECT state FROM storage_artifacts WHERE task_id=:id ORDER BY state", id=admitted["task_id"])
    assert artifacts == [{"state": "DELETED"}, {"state": "LIVE"}]
