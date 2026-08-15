from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import wait_for_job


def create_ready_dataset(client, auth_headers):
    project = client.post(
        "/api/v1/projects",
        json={"name": "Revenue analytics", "description": "Dashboard integration test"},
        headers=auth_headers,
    ).json()
    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={
            "file": (
                "revenue.csv",
                BytesIO(b"region,revenue\nSouth,100\nSouth,50\nNorth,80\n"),
                "text/csv",
            )
        },
        headers=auth_headers,
    ).json()
    wait_for_job(client, auth_headers, uploaded["task_id"])
    return project["id"], uploaded["dataset_id"]


def test_dashboard_chart_and_report_flow(client, auth_headers):
    project_id, dataset_id = create_ready_dataset(client, auth_headers)
    dashboard = client.post(
        "/api/v1/dashboards",
        json={"project_id": project_id, "name": "Revenue", "description": "Regional view"},
        headers=auth_headers,
    )
    assert dashboard.status_code == 201

    chart = client.post(
        f"/api/v1/dashboards/{dashboard.json()['id']}/charts",
        json={
            "dataset_id": dataset_id,
            "title": "Revenue by region",
            "chart_type": "bar",
            "x_column": "region",
            "y_column": "revenue",
            "aggregation": "sum",
        },
        headers=auth_headers,
    )
    assert chart.status_code == 201
    data = client.get(
        f"/api/v1/dashboards/charts/{chart.json()['id']}/data",
        headers=auth_headers,
    )
    assert data.status_code == 200
    assert dict(zip(data.json()["labels"], data.json()["values"], strict=True)) == {
        "North": 80,
        "South": 150,
    }

    report = client.post(
        f"/api/v1/reports/project/{project_id}/dataset/{dataset_id}",
        headers=auth_headers,
    )
    assert report.status_code == 202
    status = client.get(f"/api/v1/reports/{report.json()['report_id']}", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    download = client.get(status.json()["download_url"], headers=auth_headers)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"


def test_transformation_idempotency_and_version_conflict(client, auth_headers):
    _, dataset_id = create_ready_dataset(client, auth_headers)
    payload = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1}
    headers = {**auth_headers, "Idempotency-Key": "same-operation"}
    first = client.post(f"/api/v1/datasets/{dataset_id}/transform", json=payload, headers=headers)
    assert first.status_code == 202
    wait_for_job(client, auth_headers, first.json()["task_id"])

    repeated = client.post(f"/api/v1/datasets/{dataset_id}/transform", json=payload, headers=headers)
    assert repeated.status_code == 202
    assert repeated.json()["reused"] is True
    assert repeated.json()["transformation_id"] == first.json()["transformation_id"]

    stale = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "stale-version"},
    )
    assert stale.status_code == 409


def test_concurrent_transformations_cannot_commit_the_same_version(client, auth_headers):
    _, dataset_id = create_ready_dataset(client, auth_headers)
    payload = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1}

    def submit(index):
        return client.post(
            f"/api/v1/datasets/{dataset_id}/transform",
            json=payload,
            headers={**auth_headers, "Idempotency-Key": f"concurrent-{index}"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, range(2)))

    assert all(response.status_code in {202, 409} for response in responses)
    accepted = [response for response in responses if response.status_code == 202]
    assert accepted

    job_states = [
        client.get(
            f"/api/v1/datasets/jobs/{response.json()['task_id']}",
            headers=auth_headers,
        ).json()["status"]
        for response in accepted
    ]
    assert job_states.count("SUCCESS") == 1
    assert all(status in {"SUCCESS", "FAILURE"} for status in job_states)

    dataset = client.get(f"/api/v1/datasets/{dataset_id}", headers=auth_headers)
    assert dataset.json()["version"] == 2
    history = client.get(
        f"/api/v1/datasets/{dataset_id}/transformations",
        headers=auth_headers,
    ).json()
    assert sum(item["status"] == "completed" for item in history) == 1
