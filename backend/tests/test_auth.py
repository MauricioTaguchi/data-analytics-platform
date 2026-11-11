from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.api.v1.routes import auth as auth_routes
from app.db.session import SessionLocal
from app.models.session import RefreshSession
from app.models.user import User


def test_register_and_login(client):
    register = client.post(
        "/api/v1/auth/register",
        json={
            "name": "Mauricio",
            "email": "mauricio@example.com",
            "password": "password12345",
        },
    )
    assert register.status_code == 201
    assert register.json()["token_type"] == "bearer"

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "mauricio@example.com",
            "password": "password12345",
        },
    )
    assert login.status_code == 200


def test_registration_user_and_initial_session_become_visible_atomically(client, monkeypatch):
    issue_pair = auth_routes.issue_token_pair
    def inspect_before_issuance(db, user, **kwargs):
        with SessionLocal() as observer:
            assert observer.query(User).filter_by(email=user.email).first() is None
            assert observer.query(RefreshSession).count() == 0
        return issue_pair(db, user, **kwargs)
    monkeypatch.setattr(auth_routes, "issue_token_pair", inspect_before_issuance)
    response = client.post("/api/v1/auth/register", json={
        "name": "Atomic registration", "email": "atomic@example.com", "password": "password12345",
    })
    assert response.status_code == 201
    with SessionLocal() as observer:
        user = observer.query(User).filter_by(email="atomic@example.com").one()
        assert observer.query(RefreshSession).filter_by(user_id=user.id).count() == 1


def test_registration_rolls_back_account_when_initial_session_fails(client, monkeypatch):
    issue_pair = auth_routes.issue_token_pair
    def failing_pair(db, user, **kwargs):
        def fail_commit():
            # Flush both rows to exercise rollback after their actual inserts.
            db.flush()
            raise RuntimeError("Simulated session persistence failure")
        monkeypatch.setattr(db, "commit", fail_commit)
        return issue_pair(db, user, **kwargs)
    monkeypatch.setattr(auth_routes, "issue_token_pair", failing_pair)
    with pytest.raises(RuntimeError, match="Simulated session persistence failure"):
        client.post("/api/v1/auth/register", json={
            "name": "Failed registration", "email": "rollback@example.com", "password": "password12345",
        })
    with SessionLocal() as observer:
        assert observer.query(User).filter_by(email="rollback@example.com").first() is None
        assert observer.query(RefreshSession).count() == 0


def test_concurrent_registration_of_same_email_returns_conflict(client, monkeypatch):
    barrier = Barrier(2)
    hash_password = auth_routes.hash_password
    def concurrent_hash(password):
        result = hash_password(password)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(auth_routes, "hash_password", concurrent_hash)
    def register_once(_):
        return client.post("/api/v1/auth/register", json={
            "name": "Concurrent registration", "email": "duplicate@example.com", "password": "password12345",
        }).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(register_once, range(2)))
    assert sorted(statuses) == [201, 409]
    with SessionLocal() as observer:
        assert observer.query(User).count() == 1
        assert observer.query(RefreshSession).count() == 1


def test_register_and_login_reject_passwords_over_72_utf8_bytes(client):
    oversized_password = "A1" + ("é" * 36)
    register = client.post(
        "/api/v1/auth/register",
        json={
            "name": "Long Password",
            "email": "long-password@example.com",
            "password": oversized_password,
        },
    )
    assert register.status_code == 422
    assert "72 UTF-8 bytes" in str(register.json())

    client.post(
        "/api/v1/auth/register",
        json={
            "name": "Existing User",
            "email": "existing-password@example.com",
            "password": "password12345",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "existing-password@example.com",
            "password": oversized_password,
        },
    )
    assert login.status_code == 422
    assert "72 UTF-8 bytes" in str(login.json())


@pytest.mark.parametrize(
    "secret",
    [
        "too-short",
        "replace-with-a-long-random-secret",
        "replace-with-at-least-32-random-bytes",
        "placeholder-placeholder-placeholder-placeholder",
    ],
)
def test_production_rejects_short_or_placeholder_secret_keys(secret):
    with pytest.raises(ValidationError, match="at least 32 non-placeholder UTF-8 bytes"):
        Settings(
            _env_file=None,
            ENVIRONMENT="production",
            SECRET_KEY=secret,
        )


def test_production_secret_length_is_measured_in_utf8_bytes():
    configured = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        SECRET_KEY="é" * 16,
    )

    assert configured.SECRET_KEY == "é" * 16
