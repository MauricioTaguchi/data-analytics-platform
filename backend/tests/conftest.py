import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CELERY_EAGER", "true")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("AUTH_RATE_LIMIT_MAX_ATTEMPTS", "1000")

import pytest
from fastapi.testclient import TestClient
from app.db.base import Base
from app.db.session import engine
from app.main import app

@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def auth_headers(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "name": "Mauricio",
            "email": "mauricio@example.com",
            "password": "password12345",
        },
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def wait_for_job(client, headers, task_id):
    response = client.get(f"/api/v1/datasets/jobs/{task_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"
    return response.json().get("result") or {}
