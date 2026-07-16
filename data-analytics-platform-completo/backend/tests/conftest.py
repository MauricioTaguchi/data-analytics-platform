import os
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["SECRET_KEY"] = "test-secret"

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
            "password": "senha12345",
        },
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
