"""Exercise checksum failure and database guards after real Alembic migrations."""
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_corruption_and_direct_sql_mutations_cannot_restore_or_rewrite_history(runtime):
    dataset_id, headers, original = runtime.create_dataset()
    admitted = runtime.transform(dataset_id, headers)
    runtime.wait_job(admitted["task_id"], headers)
    snapshots = runtime.rows("SELECT id, stored_path, sha256 FROM dataset_snapshots WHERE dataset_id=:id ORDER BY version", id=dataset_id)
    assert len(snapshots) == 2
    path = Path(snapshots[0]["stored_path"])
    path.write_bytes(original.replace(b"Ana", b"Bob"))
    response = runtime.request(
        "POST", f"/datasets/{dataset_id}/transformations/undo", expected=409,
        headers={**headers, "Idempotency-Key": "corrupt-snapshot"}, json={"expected_version": 2},
    )
    assert "integrity verification" in response.json()["detail"]
    assert runtime.rows("SELECT version FROM datasets WHERE id=:id", id=dataset_id)[0]["version"] == 2
    assert len(runtime.rows("SELECT id FROM dataset_events WHERE dataset_id=:id", id=dataset_id)) == 1
    for query in (
        "UPDATE dataset_events SET result_json='{}' WHERE dataset_id=:id",
        "DELETE FROM dataset_events WHERE dataset_id=:id",
        "TRUNCATE dataset_events",
        "UPDATE dataset_snapshots SET sha256=repeat('0',64) WHERE dataset_id=:id",
        "DELETE FROM dataset_snapshots WHERE dataset_id=:id",
        "TRUNCATE dataset_snapshots",
    ):
        with pytest.raises(DBAPIError, match="append-only|immutable|retained for audit"):
            with runtime.database.begin() as connection:
                connection.execute(text(query), {"id": dataset_id})
    # Database guards permit exactly the retention metadata used by the API.
    snapshot = runtime.request("PATCH", f"/datasets/{dataset_id}/snapshots/{snapshots[0]['id']}",
                               headers=headers, json={"pinned": True}).json()
    assert snapshot["pinned"] is True
    path.write_bytes(original)
    undo = runtime.request("POST", f"/datasets/{dataset_id}/transformations/undo", headers=headers,
                           json={"expected_version": 2}).json()
    assert undo["snapshot_integrity"] == "verified"
