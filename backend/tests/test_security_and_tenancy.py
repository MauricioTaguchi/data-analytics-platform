from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import wait_for_job
from app.core.config import settings
from app.core.cache import CacheService


def register(client, email):
    response = client.post("/api/v1/auth/register", json={"name": "Data User", "email": email, "password": "securepass123"})
    assert response.status_code == 201
    return response.json()


def test_refresh_rotation_and_logout(client):
    tokens = register(client, "session@example.com")
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401
    logout = client.post("/api/v1/auth/logout", json={"refresh_token": refreshed.json()["refresh_token"]})
    assert logout.status_code == 204


def test_refresh_token_can_only_be_rotated_once_under_concurrency(client):
    tokens = register(client, "concurrent-session@example.com")

    def rotate():
        return client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _: rotate(), range(2)))

    assert statuses == [200, 401]


def test_dataset_isolation_and_mime_validation(client):
    owner = register(client, "owner@example.com")
    stranger = register(client, "stranger@example.com")
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    stranger_headers = {"Authorization": f"Bearer {stranger['access_token']}"}
    project = client.post("/api/v1/projects", json={"name": "Private", "description": "Tenant test"}, headers=owner_headers).json()
    invalid = client.post(f"/api/v1/datasets/project/{project['id']}", files={"file": ("fake.csv", BytesIO(b"a,b\n1,2"), "image/png")}, headers=owner_headers)
    assert invalid.status_code == 400
    uploaded = client.post(f"/api/v1/datasets/project/{project['id']}", files={"file": ("valid.csv", BytesIO(b"a,b\n1,2"), "text/csv")}, headers=owner_headers)
    assert uploaded.status_code == 202
    dataset_id = uploaded.json()["dataset_id"]
    wait_for_job(client, owner_headers, uploaded.json()["task_id"])
    forbidden = client.get(f"/api/v1/datasets/{dataset_id}/preview", headers=stranger_headers)
    assert forbidden.status_code == 404


def test_malformed_dataset_fails_in_background(client):
    owner = register(client, "malformed@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Validation", "description": "Malformed input test"},
        headers=headers,
    ).json()
    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("invalid.csv", BytesIO(b'"unterminated'), "text/csv")},
        headers=headers,
    )
    assert uploaded.status_code == 202
    job = client.get(f"/api/v1/datasets/jobs/{uploaded.json()['task_id']}", headers=headers)
    assert job.status_code == 200
    assert job.json()["status"] == "FAILURE"


def test_streaming_upload_rejects_files_over_the_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 1)
    owner = register(client, "large-file@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Bounded uploads", "description": "Streaming size test"},
        headers=headers,
    ).json()
    oversized = b"column\n" + (b"1\n" * 600_000)
    response = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("large.csv", BytesIO(oversized), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 400
    assert "size limit" in response.json()["detail"]


def test_authentication_rate_limit_uses_the_shared_counter(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_MAX_ATTEMPTS", 2)
    CacheService.delete("rate-limit:auth:testclient")
    payload = {"email": "missing@example.com", "password": "password12345"}

    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    limited = client.post("/api/v1/auth/login", json=payload)

    assert limited.status_code == 429
