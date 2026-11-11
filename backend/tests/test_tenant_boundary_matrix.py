"""Exercise hostile resource identifiers through the public HTTP boundary."""

import pytest

from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.models.chart import Chart
from app.models.dashboard import Dashboard
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.project import Project
from app.models.report import Report
from app.models.user import User


@pytest.fixture
def private_resources(tmp_path):
    source = tmp_path / "private.csv"
    source.write_text("confidential\nnot-for-other-accounts\n", encoding="utf-8")
    report_file = tmp_path / "private.pdf"
    report_file.write_bytes(b"%PDF-private-report")
    with SessionLocal() as db:
        owner = User(name="Owner", email="boundary-owner@example.com", password_hash="unused")
        stranger = User(name="Other", email="boundary-other@example.com", password_hash="unused")
        db.add_all([owner, stranger])
        db.flush()
        project = Project(name="Private project", owner_id=owner.id)
        db.add(project)
        db.flush()
        dataset = Dataset(project_id=project.id, original_filename="private.csv",
                          stored_path=str(source), status="ready", row_count=1, column_count=1)
        dashboard = Dashboard(project_id=project.id, name="Private dashboard")
        db.add_all([dataset, dashboard])
        db.flush()
        report = Report(project_id=project.id, dataset_id=dataset.id,
                        status="completed", file_path=str(report_file))
        chart = Chart(dashboard_id=dashboard.id, dataset_id=dataset.id,
                      title="Private count", chart_type="kpi", aggregation="count")
        db.add_all([report, chart])
        db.flush()
        job = JobRecord(task_id="private-task", owner_id=owner.id,
                        dataset_id=dataset.id, kind="profile", status="PENDING")
        db.add(job)
        db.commit()
        ids = {"project": project.id, "dataset": dataset.id, "dashboard": dashboard.id,
               "report": report.id, "chart": chart.id, "job": job.task_id}
        headers = {"Authorization": f"Bearer {create_access_token(str(stranger.id))}"}
    return ids, headers, source, report_file


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/projects/{project}", None),
    ("GET", "/datasets/project/{project}", None),
    ("GET", "/datasets/{dataset}", None),
    ("GET", "/datasets/{dataset}/preview", None),
    ("GET", "/datasets/{dataset}/profile", None),
    ("POST", "/datasets/{dataset}/profile", None),
    ("GET", "/datasets/{dataset}/export", None),
    ("GET", "/datasets/{dataset}/events", None),
    ("GET", "/datasets/{dataset}/transformations", None),
    ("POST", "/datasets/{dataset}/transformations/undo", {"expected_version": 1}),
    ("GET", "/datasets/{dataset}/snapshots", None),
    ("GET", "/datasets/{dataset}/quality/contracts", None),
    ("GET", "/datasets/{dataset}/quality/evaluations", None),
    ("GET", "/datasets/jobs/{job}", None),
    ("DELETE", "/datasets/jobs/{job}", None),
    ("GET", "/reports/project/{project}", None),
    ("GET", "/reports/{report}", None),
    ("GET", "/reports/{report}/download", None),
    ("DELETE", "/reports/{report}", None),
    ("GET", "/dashboards/project/{project}", None),
    ("GET", "/dashboards/{dashboard}/charts", None),
    ("GET", "/dashboards/charts/{chart}/data", None),
])
def test_foreign_resources_are_concealed_and_unchanged(client, private_resources, method, path, payload):
    ids, headers, source, report_file = private_resources
    response = client.request(method, "/api/v1" + path.format(**ids), headers=headers, json=payload)
    assert response.status_code == 404, response.text
    assert "not-for-other-accounts" not in response.text
    assert str(source) not in response.text
    assert source.read_text(encoding="utf-8") == "confidential\nnot-for-other-accounts\n"
    assert report_file.read_bytes() == b"%PDF-private-report"
    with SessionLocal() as db:
        assert db.get(Dataset, ids["dataset"]).version == 1
        assert db.get(JobRecord, ids["job"]).status == "PENDING"
        assert db.get(Report, ids["report"]).status == "completed"
        assert db.query(JobRecord).count() == 1


def test_foreign_chart_and_unknown_chart_have_identical_response(client, private_resources):
    ids, headers, _, _ = private_resources
    foreign = client.get(f"/api/v1/dashboards/charts/{ids['chart']}/data", headers=headers)
    missing = client.get("/api/v1/dashboards/charts/2147483647/data", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
