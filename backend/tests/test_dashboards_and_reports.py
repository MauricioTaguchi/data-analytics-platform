from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy.orm import Session as OrmSession

from app.core.config import settings
from app.models.report import Report
from app.models.transformation import Transformation
from app.services.dashboard_service import DashboardService
from app.services.dataset_service import DatasetService
from app.services.storage_service import storage
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


def test_profile_bounds_correlation_width_and_rejects_oversized_headers(monkeypatch):
    dataset = SimpleNamespace(stored_path="unused.csv")
    monkeypatch.setattr(settings, "MAX_PROFILE_CORRELATION_COLUMNS", 2)
    monkeypatch.setattr(
        DatasetService,
        "read_dataframe",
        lambda *_args: pd.DataFrame({"one": [1, 2], "two": [2, 4], "three": [3, 6]}),
    )

    profile = DatasetService.build_profile(dataset)

    assert set(profile["correlations"]) == {"one", "two"}
    assert profile["correlation_columns_analyzed"] == 2
    assert profile["correlation_columns_truncated"] == 1

    monkeypatch.setattr(
        DatasetService,
        "read_dataframe",
        lambda *_args: pd.DataFrame({"x" * (settings.MAX_DATASET_COLUMN_NAME_CHARS + 1): [1]}),
    )
    with pytest.raises(ValueError, match="column names"):
        DatasetService.build_profile(dataset)


def test_dataset_preview_bounds_width_and_cell_values(monkeypatch):
    dataset = SimpleNamespace(stored_path="unused.csv", status="ready")
    monkeypatch.setattr(settings, "TRANSFORMATION_PREVIEW_MAX_COLUMNS", 1)
    monkeypatch.setattr(settings, "TRANSFORMATION_PREVIEW_MAX_CELL_CHARS", 4)
    monkeypatch.setattr(
        DatasetService,
        "read_dataframe",
        lambda *_args: pd.DataFrame({"visible": ["abcdefgh"], "hidden": [1]}),
    )

    preview = DatasetService.preview(dataset, page=1, page_size=20)

    assert preview["columns"] == ["visible"]
    assert preview["rows"] == [{"visible": "abcd..."}]
    assert preview["total_columns"] == 2
    assert preview["columns_truncated"] == 1


def test_grouped_chart_data_omits_nan_but_preserves_zero(monkeypatch):
    dataset = SimpleNamespace(stored_path="unused.csv")
    frame = pd.DataFrame(
        {
            "region": ["Empty", "Empty", "Filled"],
            "revenue": [None, None, 5.0],
        }
    )
    monkeypatch.setattr(
        DashboardService,
        "ensure_dataset_for_dashboard",
        classmethod(lambda *_args, **_kwargs: dataset),
    )
    monkeypatch.setattr(DatasetService, "read_dataframe", lambda *_args: frame)

    chart = SimpleNamespace(
        dashboard_id=1,
        dataset_id=1,
        filters_json={},
        chart_type="bar",
        x_column="region",
        y_column="revenue",
        title="Revenue",
    )
    for aggregation in ("mean", "min", "max"):
        chart.aggregation = aggregation
        result = DashboardService.build_chart_data(None, chart, owner_id=1)
        assert result["labels"] == ["Filled"]
        assert result["values"] == [5.0]

    for aggregation in ("sum", "count"):
        chart.aggregation = aggregation
        result = DashboardService.build_chart_data(None, chart, owner_id=1)
        assert result["labels"] == ["Empty", "Filled"]
        assert result["values"][0] == 0

    monkeypatch.setattr(
        DatasetService,
        "read_dataframe",
        lambda *_args: pd.DataFrame({"revenue": [float("nan"), float("nan")]}),
    )
    chart.chart_type = "kpi"
    chart.x_column = None
    chart.y_column = "revenue"
    chart.aggregation = "mean"

    result = DashboardService.build_chart_data(None, chart, owner_id=1)

    assert result["values"] == [None]


def test_dashboard_contains_filter_treats_input_as_literal_text():
    frame = pd.DataFrame({"label": ["a.b", "axb"]})

    result = DashboardService._apply_filters(
        frame,
        {"label": {"contains": "."}},
    )

    assert result["label"].tolist() == ["a.b"]


def test_dashboard_chart_and_report_flow(client, auth_headers, monkeypatch, tmp_path):
    requested_task_id = "123e4567-e89b-12d3-a456-426614174001"
    monkeypatch.setattr(storage, "root", tmp_path / "uploads")
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))
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
        headers={**auth_headers, "X-Task-ID": requested_task_id},
    )
    assert report.status_code == 202
    assert report.json()["task_id"] == requested_task_id
    status = client.get(f"/api/v1/reports/{report.json()['report_id']}", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    download = client.get(status.json()["download_url"], headers=auth_headers)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    deleted = client.delete(
        f"/api/v1/reports/{report.json()['report_id']}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204
    assert client.get(status.json()["download_url"], headers=auth_headers).status_code == 404


def test_report_commit_ack_loss_preserves_the_committed_pdf(
    client,
    auth_headers,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(storage, "root", tmp_path / "uploads")
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))
    project_id, dataset_id = create_ready_dataset(client, auth_headers)
    original_commit = OrmSession.commit
    acknowledgement_lost = False

    def commit_with_lost_ack(session):
        nonlocal acknowledgement_lost
        lose_this_ack = not acknowledgement_lost and any(
            isinstance(item, Report) and item.status == "completed"
            for item in session.identity_map.values()
        )
        original_commit(session)
        if lose_this_ack:
            acknowledgement_lost = True
            raise ConnectionError("database acknowledgement was lost")

    monkeypatch.setattr(OrmSession, "commit", commit_with_lost_ack)

    queued = client.post(
        f"/api/v1/reports/project/{project_id}/dataset/{dataset_id}",
        headers=auth_headers,
    )

    assert queued.status_code == 202
    assert acknowledgement_lost is True
    status = client.get(
        f"/api/v1/reports/{queued.json()['report_id']}",
        headers=auth_headers,
    )
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    download = client.get(status.json()["download_url"], headers=auth_headers)
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")


@pytest.mark.parametrize(
    ("commit_succeeds", "expected_job", "expected_transformation", "expected_version"),
    [
        (True, "SUCCESS", "completed", 2),
        (False, "FAILURE", "failed", 1),
    ],
)
def test_transformation_final_commit_is_reconciled_without_orphaning_output(
    client,
    auth_headers,
    monkeypatch,
    tmp_path,
    commit_succeeds,
    expected_job,
    expected_transformation,
    expected_version,
):
    monkeypatch.setattr(storage, "root", tmp_path)
    _project_id, dataset_id = create_ready_dataset(client, auth_headers)
    original_commit = OrmSession.commit
    injected = False

    def controlled_final_commit(session):
        nonlocal injected
        target_commit = not injected and any(
            isinstance(item, Transformation) and item.status == "completed"
            for item in session.identity_map.values()
        )
        if target_commit:
            injected = True
            if commit_succeeds:
                original_commit(session)
                raise ConnectionError("database acknowledgement was lost")
            raise RuntimeError("database rejected the final commit")
        original_commit(session)

    monkeypatch.setattr(OrmSession, "commit", controlled_final_commit)
    queued = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json={"operation": "drop_duplicates", "parameters": {}, "expected_version": 1},
        headers={**auth_headers, "Idempotency-Key": f"commit-{commit_succeeds}"},
    )

    assert queued.status_code == 202
    assert injected is True
    job = client.get(
        f"/api/v1/datasets/jobs/{queued.json()['task_id']}",
        headers=auth_headers,
    )
    assert job.status_code == 200
    assert job.json()["status"] == expected_job
    dataset = client.get(f"/api/v1/datasets/{dataset_id}", headers=auth_headers)
    assert dataset.json()["status"] == "ready"
    assert dataset.json()["version"] == expected_version
    history = client.get(
        f"/api/v1/datasets/{dataset_id}/transformations",
        headers=auth_headers,
    ).json()
    assert history[0]["status"] == expected_transformation
    csv_artifacts = list(tmp_path.glob("*.csv"))
    assert len(csv_artifacts) == (2 if commit_succeeds else 1)


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
    assert repeated.json()["task_id"] == first.json()["task_id"]

    conflicting = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json={"operation": "fill_nulls", "parameters": {"values": {}}, "expected_version": 1},
        headers=headers,
    )
    assert conflicting.status_code == 409
    assert "different transformation request" in conflicting.json()["detail"]

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
