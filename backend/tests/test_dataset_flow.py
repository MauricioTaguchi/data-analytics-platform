from io import BytesIO

from tests.conftest import wait_for_job


def test_complete_dataset_flow(client, auth_headers):
    project = client.post(
        "/api/v1/projects",
        json={
            "name": "Sales",
            "description": "Commercial analysis",
        },
        headers=auth_headers,
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    csv_content = (
        b"name,age,salary\n"
        b"Ana,30,5000\n"
        b"Bruno,40,7000\n"
        b"Bruno,40,7000\n"
    )

    upload = client.post(
        f"/api/v1/datasets/project/{project_id}",
        files={
            "file": (
                "sample.csv",
                BytesIO(csv_content),
                "text/csv",
            )
        },
        headers=auth_headers,
    )
    assert upload.status_code == 202
    dataset_id = upload.json()["dataset_id"]
    wait_for_job(client, auth_headers, upload.json()["task_id"])

    preview = client.get(
        f"/api/v1/datasets/{dataset_id}/preview",
        headers=auth_headers,
    )
    assert preview.status_code == 200
    assert preview.json()["total_rows"] == 3

    profile = client.post(
        f"/api/v1/datasets/{dataset_id}/profile",
        headers=auth_headers,
    )
    assert profile.status_code == 202
    assert profile.json()["status"] in {"PENDING", "SUCCESS"}
    assert "task_id" in profile.json()

    transformation_preview = client.post(
        f"/api/v1/datasets/{dataset_id}/transform/preview",
        json={"operation": "drop_duplicates", "parameters": {}, "expected_version": 1},
        headers=auth_headers,
    )
    assert transformation_preview.status_code == 202
    preview_result = wait_for_job(
        client,
        auth_headers,
        transformation_preview.json()["task_id"],
    )
    assert preview_result["before"]["rows"] == 3
    assert preview_result["after"]["rows"] == 2

    transform = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json={
            "operation": "drop_duplicates",
            "parameters": {},
            "expected_version": 1,
        },
        headers=auth_headers,
    )
    assert transform.status_code == 422

    transform = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json={"operation": "drop_duplicates", "parameters": {}, "expected_version": 1},
        headers={**auth_headers, "Idempotency-Key": "deduplicate-v1"},
    )
    assert transform.status_code == 202
    result = wait_for_job(client, auth_headers, transform.json()["task_id"])
    assert result["before_rows"] == 3
    assert result["after_rows"] == 2

    history = client.get(
        f"/api/v1/datasets/{dataset_id}/transformations",
        headers=auth_headers,
    )
    assert history.status_code == 200
    assert history.json()[0]["operation"] == "drop_duplicates"
    assert history.json()[0]["status"] == "completed"

    undo = client.post(
        f"/api/v1/datasets/{dataset_id}/transformations/undo",
        headers=auth_headers,
    )
    assert undo.status_code == 200
    assert undo.json()["status"] == "undone"

    restored = client.get(
        f"/api/v1/datasets/{dataset_id}/preview",
        headers=auth_headers,
    )
    assert restored.json()["total_rows"] == 3
