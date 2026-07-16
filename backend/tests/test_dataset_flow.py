from io import BytesIO


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
    assert upload.status_code == 201
    dataset_id = upload.json()["id"]

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
    assert profile.json()["status"] in {"queued", "completed"}
    assert "task_id" in profile.json()

    transform = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json={
            "operation": "drop_duplicates",
            "parameters": {},
        },
        headers=auth_headers,
    )
    assert transform.status_code == 202