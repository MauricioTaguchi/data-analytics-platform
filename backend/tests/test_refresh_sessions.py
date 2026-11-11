from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import jwt
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.api.v1.routes import auth as auth_routes
from app.core.config import settings
from app.core.security import ALGORITHM, create_refresh_token
from app.db.session import SessionLocal
from app.models.session import RefreshSession
from app.schemas.auth import RefreshRequest


def register(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"name": "Session owner", "email": "sessions@example.com", "password": "securepass123"},
    )
    assert response.status_code == 201
    return response.json()


def refresh(client, token):
    return client.post("/api/v1/auth/refresh", json={"refresh_token": token})


def claims(token):
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


def independent_login(client):
    response = client.post(
        "/api/v1/auth/login", json={"email": "sessions@example.com", "password": "securepass123"}
    )
    assert response.status_code == 200
    return response.json()["refresh_token"]


def test_replaying_ancestor_revokes_latest_descendant_only(client):
    original = register(client)["refresh_token"]
    other_device = independent_login(client)
    first = refresh(client, original).json()["refresh_token"]
    second = refresh(client, first).json()["refresh_token"]

    assert refresh(client, original).status_code == 401
    assert refresh(client, second).status_code == 401
    assert refresh(client, other_device).status_code == 200
    with SessionLocal() as db:
        family = db.query(RefreshSession).filter_by(jti=claims(original)["jti"]).one().family_id
        sessions = db.query(RefreshSession).filter_by(family_id=family).all()
        assert len(sessions) == 3
        assert all(session.revoked_at is not None for session in sessions)


@pytest.mark.parametrize("logout_ancestor", [False, True])
def test_logout_revokes_family_and_preserves_independent_login(client, logout_ancestor):
    original = register(client)["refresh_token"]
    other_device = independent_login(client)
    current = refresh(client, original).json()["refresh_token"]
    logout_token = original if logout_ancestor else current

    assert client.post("/api/v1/auth/logout", json={"refresh_token": logout_token}).status_code == 204
    assert client.post("/api/v1/auth/logout", json={"refresh_token": logout_token}).status_code == 204
    assert refresh(client, current).status_code == 401
    assert refresh(client, other_device).status_code == 200


@pytest.mark.parametrize("operation", ["refresh", "logout"])
@pytest.mark.parametrize(
    "invalid_claims",
    [
        {"exp": 1},
        {"sub": "999"},
        {"sub": "invalid"},
        {"sub": "9" * 5000},
        {"sub": "9999999999999999999"},
        {"jti": "unknown-session"},
        {"jti": ["invalid"]},
        {"type": "access"},
        {"exp": None},
        {"exp": []},
    ],
)
def test_invalid_token_cannot_revoke_live_family(client, operation, invalid_claims):
    original = register(client)["refresh_token"]
    current = refresh(client, original).json()["refresh_token"]
    payload = {**claims(original), **invalid_claims}
    if payload["exp"] is None:
        del payload["exp"]
    invalid = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

    response = client.post(f"/api/v1/auth/{operation}", json={"refresh_token": invalid})
    assert response.status_code in (401, 204)
    assert refresh(client, current).status_code == 200


@pytest.mark.parametrize("operation", ["refresh", "logout"])
def test_unsigned_or_malformed_token_cannot_revoke_live_family(client, operation):
    original = register(client)["refresh_token"]
    current = refresh(client, original).json()["refresh_token"]
    forged = jwt.encode(claims(original), "incorrect-signing-key", algorithm=ALGORITHM)
    for invalid in ["invalid-token", forged]:
        assert client.post(f"/api/v1/auth/{operation}", json={"refresh_token": invalid}).status_code == 401
    assert refresh(client, current).status_code == 200


@pytest.mark.parametrize("operation", ["refresh", "logout"])
def test_database_expired_ancestor_cannot_revoke_live_family(client, operation):
    original = register(client)["refresh_token"]
    current = refresh(client, original).json()["refresh_token"]
    with SessionLocal() as db:
        db.query(RefreshSession).filter_by(jti=claims(original)["jti"]).update(
            {RefreshSession.expires_at: datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()

    response = client.post(f"/api/v1/auth/{operation}", json={"refresh_token": original})
    assert response.status_code == (401 if operation == "refresh" else 204)
    assert refresh(client, current).status_code == 200


def test_simultaneous_reuse_issues_one_successor_then_revokes_it(client, monkeypatch):
    original = register(client)["refresh_token"]
    barrier = Barrier(2)
    lock_session = auth_routes._lock_refresh_session

    def simultaneous_lock(db, token_claims):
        barrier.wait(timeout=10)
        return lock_session(db, token_claims)

    monkeypatch.setattr(auth_routes, "_lock_refresh_session", simultaneous_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: refresh(client, original), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 401]
    successor = next(response.json()["refresh_token"] for response in responses if response.status_code == 200)
    monkeypatch.setattr(auth_routes, "_lock_refresh_session", lock_session)
    assert refresh(client, successor).status_code == 401
    with SessionLocal() as db:
        assert db.query(RefreshSession).count() == 2
        assert db.query(RefreshSession).filter(RefreshSession.revoked_at.is_(None)).count() == 0


@pytest.mark.parametrize("first_operation", ["replay", "rotate", "logout"])
def test_family_revocation_serializes_with_successor_rotation(client, monkeypatch, first_operation):
    original = register(client)["refresh_token"]
    current = refresh(client, original).json()["refresh_token"]
    first_token = current if first_operation == "rotate" else original
    second_token = original if first_operation == "rotate" else current
    first_endpoint = "logout" if first_operation == "logout" else "refresh"
    first_jti = claims(first_token)["jti"]
    first_locked, second_started, release_first = Event(), Event(), Event()
    lock_session = auth_routes._lock_refresh_session

    def ordered_lock(db, token_claims):
        if token_claims["jti"] != first_jti:
            second_started.set()
            return lock_session(db, token_claims)
        session = lock_session(db, token_claims)
        first_locked.set()
        assert release_first.wait(timeout=10)
        return session

    monkeypatch.setattr(auth_routes, "_lock_refresh_session", ordered_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            client.post, f"/api/v1/auth/{first_endpoint}", json={"refresh_token": first_token}
        )
        try:
            assert first_locked.wait(timeout=10)
            second_future = executor.submit(refresh, client, second_token)
            assert second_started.wait(timeout=10)
        finally:
            release_first.set()
        first, second = first_future.result(timeout=10), second_future.result(timeout=10)

    assert first.status_code == {"replay": 401, "rotate": 200, "logout": 204}[first_operation]
    assert second.status_code == 401
    with SessionLocal() as db:
        assert db.query(RefreshSession).filter(RefreshSession.revoked_at.is_(None)).count() == 0
    if first_operation == "rotate":
        monkeypatch.setattr(auth_routes, "_lock_refresh_session", lock_session)
        assert refresh(client, first.json()["refresh_token"]).status_code == 401


def test_migration_preserves_legacy_sessions_and_active_tokens(tmp_path, monkeypatch):
    backend_root = Path(__file__).parents[1]
    database_url = f"sqlite:///{(tmp_path / 'legacy-sessions.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    command.upgrade(config, "20261111_0016")
    migration_engine = create_engine(database_url)
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    revoked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, name, email, password_hash, is_active) "
                    "VALUES (1, 'Legacy', 'legacy@example.com', 'hash', 1)"
                )
            )
            for jti, revoked in [("legacy-active", None), ("legacy-revoked", revoked_at)]:
                connection.execute(
                    text(
                        "INSERT INTO refresh_sessions (user_id, jti, expires_at, revoked_at) "
                        "VALUES (1, :jti, :expires_at, :revoked_at)"
                    ),
                    {"jti": jti, "expires_at": expires_at, "revoked_at": revoked},
                )
        command.upgrade(config, "20261111_0017")
        with migration_engine.connect() as connection:
            rows = connection.execute(
                text("SELECT jti, family_id, consumed_at, revoked_at FROM refresh_sessions ORDER BY id")
            ).all()
            assert rows[0] == ("legacy-active", "legacy-active", None, None)
            assert rows[1][:3] == ("legacy-revoked", "legacy-revoked", None)
            assert rows[1][3] is not None
        family_column = next(
            column for column in inspect(migration_engine).get_columns("refresh_sessions")
            if column["name"] == "family_id"
        )
        assert not family_column["nullable"]

        with Session(migration_engine) as db:
            rotated = auth_routes.refresh(
                RefreshRequest(refresh_token=create_refresh_token("1", "legacy-active")), db
            )
            successor = db.query(RefreshSession).filter_by(jti=claims(rotated.refresh_token)["jti"]).one()
            assert successor.family_id == "legacy-active"
            assert successor.revoked_at is None
        command.downgrade(config, "20261111_0016")
        with migration_engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM refresh_sessions")) == 3
    finally:
        migration_engine.dispose()
