from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from email.parser import BytesParser
from email.policy import default as email_policy
from io import BytesIO
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingTCPServer
from threading import Barrier, Event, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, text

from app.core.config import Settings, settings
from app.api.v1.routes import auth as auth_routes
from app.core.security import ALGORITHM
from app.db.session import SessionLocal
from app.models.password_reset import PasswordResetToken
from app.models.session import RefreshSession
from app.models.user import User
from app.services import password_reset_service as recovery

PASSWORD = "OriginalPassword123"
NEW_PASSWORD = "ReplacementPassword456"


@pytest.fixture
def delivery(monkeypatch):
    messages = []
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "accounts@example.com")
    monkeypatch.setattr(settings, "PASSWORD_RESET_URL", "https://app.example.com/")
    monkeypatch.setattr(recovery, "deliver_reset_link", lambda email, token: messages.append((email, token)))
    monkeypatch.setattr(recovery, "notify_password_changed", lambda email: None)
    return messages


def register(client, *, email=None):
    email = email or f"reset-{uuid4().hex}@example.com"
    response = client.post("/api/v1/auth/register", json={"name": "Recovery user", "email": email, "password": PASSWORD})
    assert response.status_code == 201
    return email, response.json()


def request_link(client, email):
    return client.post("/api/v1/auth/password-reset/request", json={"email": email})


def confirm(client, token, password=NEW_PASSWORD):
    return client.post("/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": password})


def test_request_is_uniform_for_existing_unknown_and_inactive_accounts(client, delivery):
    existing, original = register(client)
    inactive, _ = register(client)
    with SessionLocal() as db:
        db.query(User).filter_by(email=inactive).update({User.is_active: False})
        db.commit()
    responses = [request_link(client, email) for email in [existing, f"missing-{uuid4().hex}@example.com", inactive]]
    assert all(response.status_code == 202 for response in responses)
    assert all(response.json() == {"message": recovery.RESET_REQUEST_MESSAGE} for response in responses)
    assert len(delivery) == 1 and delivery[0][0] == existing
    with SessionLocal() as db:
        token = db.query(PasswordResetToken).one()
        assert token.token_hash == sha256(delivery[0][1].encode()).hexdigest()
        assert delivery[0][1] not in str(token.__dict__)
        assert token.delivered_at is not None and token.used_at is None
        assert db.query(RefreshSession).filter(RefreshSession.revoked_at.is_not(None)).count() == 0
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {original['access_token']}"}).status_code == 200


def test_reset_changes_password_revokes_access_refresh_and_other_links(client, delivery, monkeypatch):
    email, original = register(client)
    other_session = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()
    request_link(client, email)
    first_token = delivery[-1][1]
    monkeypatch.setattr(recovery.CacheService, "increment", lambda *_: 1)
    request_link(client, email)
    second_token = delivery[-1][1]
    assert confirm(client, first_token).status_code == 204
    assert confirm(client, first_token).status_code == 400
    assert confirm(client, second_token).status_code == 400
    for session in [original, other_session]:
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"}).status_code == 401
        assert client.post("/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401
    rejected_upload = client.post(
        "/api/v1/datasets/project/1", headers={"Authorization": f"Bearer {original['access_token']}"},
        files={"file": ("sample.csv", BytesIO(b"value\n1\n"), "text/csv")},
    )
    assert rejected_upload.status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401
    signed_in = client.post("/api/v1/auth/login", json={"email": email, "password": NEW_PASSWORD})
    assert signed_in.status_code == 200
    account = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"})
    assert account.status_code == 200
    assert set(account.json()) == {"id", "name", "email", "created_at"}
    assert jwt.decode(signed_in.json()["access_token"], settings.SECRET_KEY, algorithms=[ALGORITHM])["ver"] == 1


def test_expired_undelivered_used_and_unknown_tokens_have_one_error(client, delivery):
    email, _ = register(client)
    request_link(client, email)
    raw = delivery[-1][1]
    for update in [
        {PasswordResetToken.expires_at: datetime.now(timezone.utc) - timedelta(seconds=1)},
        {PasswordResetToken.expires_at: datetime.now(timezone.utc) + timedelta(minutes=5), PasswordResetToken.delivered_at: None},
        {PasswordResetToken.delivered_at: datetime.now(timezone.utc), PasswordResetToken.used_at: datetime.now(timezone.utc)},
    ]:
        with SessionLocal() as db:
            db.query(PasswordResetToken).update(update)
            db.commit()
        response = confirm(client, raw)
        assert response.status_code == 400
        assert response.json() == {"detail": recovery.INVALID_RESET_MESSAGE}
    for token in ["x" * 43, "not-a-token"]:
        assert confirm(client, token).json() == {"detail": recovery.INVALID_RESET_MESSAGE}
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200


def test_delivery_failure_returns_uniform_response_and_invalidates_link(client, delivery, monkeypatch, caplog):
    email, original = register(client)
    attempted = []
    def fail_delivery(recipient, token):
        attempted.append(token)
        raise OSError(f"secret smtp password and recipient {recipient} and token {token}")
    monkeypatch.setattr(recovery, "deliver_reset_link", fail_delivery)
    response = request_link(client, email)
    assert response.status_code == 202
    assert response.json() == {"message": recovery.RESET_REQUEST_MESSAGE}
    assert confirm(client, attempted[0]).status_code == 400
    assert email not in caplog.text and attempted[0] not in caplog.text and "secret smtp password" not in caplog.text
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {original['access_token']}"}).status_code == 200
    with SessionLocal() as db:
        record = db.query(PasswordResetToken).one()
        assert record.used_at is not None and record.delivered_at is None


def test_missing_delivery_configuration_does_not_issue_token(client, delivery, monkeypatch):
    email, _ = register(client)
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    assert request_link(client, email).status_code == 202
    assert not delivery
    with SessionLocal() as db:
        assert db.query(PasswordResetToken).count() == 0


def test_per_account_cooldown_does_not_invalidate_previously_sent_link(client, delivery):
    email, _ = register(client)
    for _ in range(3):
        assert request_link(client, email).status_code == 202
    assert len(delivery) == 1
    assert confirm(client, delivery[0][1]).status_code == 204


def test_same_link_is_consumed_once_under_concurrency(client, delivery, monkeypatch):
    email, _ = register(client)
    request_link(client, email)
    token = delivery[-1][1]
    barrier = Barrier(2)
    hash_password = recovery.hash_password
    def concurrent_hash(password):
        result = hash_password(password)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(recovery, "hash_password", concurrent_hash)
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: confirm(client, token).status_code, range(2)))
    assert sorted(statuses) == [204, 400]
    with SessionLocal() as db:
        assert db.query(User).filter_by(email=email).one().auth_version == 1


def test_login_committed_before_reset_cannot_issue_valid_access_after_reset(client, delivery, monkeypatch):
    email, _ = register(client)
    request_link(client, email)
    token = delivery[-1][1]
    committed, release = Event(), Event()
    issue_pair = auth_routes.issue_token_pair
    def delayed_pair(db, user, **kwargs):
        commit = db.commit
        def pause_after_commit():
            commit()
            committed.set()
            assert release.wait(timeout=10)
        monkeypatch.setattr(db, "commit", pause_after_commit)
        return issue_pair(db, user, **kwargs)
    monkeypatch.setattr(auth_routes, "issue_token_pair", delayed_pair)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        try:
            assert committed.wait(timeout=10)
            assert confirm(client, token).status_code == 204
        finally:
            release.set()
        signed_in = pending.result(timeout=10)
    assert signed_in.status_code == 200
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {signed_in.json()['access_token']}"}).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": signed_in.json()["refresh_token"]}).status_code == 401


def test_legacy_access_without_version_remains_valid_until_password_reset(client, delivery):
    email, session = register(client)
    payload = jwt.decode(session["access_token"], settings.SECRET_KEY, algorithms=[ALGORITHM])
    del payload["ver"]
    legacy = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    headers = {"Authorization": f"Bearer {legacy}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    request_link(client, email)
    assert confirm(client, delivery[-1][1]).status_code == 204
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


@pytest.mark.parametrize("password", ["short1", "letterswithoutdigits", "123456789000", "A1" + "é" * 36])
def test_reset_uses_registration_password_policy(client, delivery, password):
    email, _ = register(client)
    request_link(client, email)
    token = delivery[-1][1]
    response = confirm(client, token, password)
    assert response.status_code == 422
    assert token not in response.text and password not in response.text
    assert confirm(client, token).status_code == 204


def test_reset_endpoints_share_auth_ip_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(recovery.CacheService, "increment", lambda *_: 2)
    assert request_link(client, "someone@example.com").status_code == 429
    assert confirm(client, "x" * 43).status_code == 429


def test_smtp_uses_verified_starttls_and_does_not_log_credentials(monkeypatch):
    calls = []
    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def starttls(self, *, context):
            assert context.check_hostname
            calls.append(("tls",))
        def login(self, username, password):
            calls.append(("login", username, password))
        def send_message(self, message):
            calls.append(("message", message))
            return {}
    monkeypatch.setattr(recovery.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "mailer")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", SecretStr("smtp-test-secret"))
    monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "accounts@example.com")
    monkeypatch.setattr(settings, "PASSWORD_RESET_URL", "https://app.example.com/")
    recovery.deliver_reset_link("recipient@example.com", "t" * 43)
    assert [call[0] for call in calls] == ["connect", "tls", "login", "message"]
    message = calls[-1][1]
    assert "#reset_token=" in message.get_content()
    assert "smtp-test-secret" not in str(message)


def test_local_smtp_roundtrip_recovers_account_without_external_delivery(client, monkeypatch):
    received = []
    class LocalSMTPHandler(StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 localhost recovery test\r\n")
            while line := self.rfile.readline():
                command_name = line.split(b" ", 1)[0].strip().upper()
                if command_name in {b"EHLO", b"HELO"}:
                    self.wfile.write(b"250 localhost\r\n")
                elif command_name in {b"MAIL", b"RCPT", b"RSET"}:
                    self.wfile.write(b"250 OK\r\n")
                elif command_name == b"DATA":
                    self.wfile.write(b"354 Send message\r\n")
                    body = []
                    while (data := self.rfile.readline()) != b".\r\n":
                        if not data:
                            return
                        body.append(data[1:] if data.startswith(b"..") else data)
                    received.append(BytesParser(policy=email_policy).parsebytes(b"".join(body)))
                    self.wfile.write(b"250 Queued locally\r\n")
                elif command_name == b"QUIT":
                    self.wfile.write(b"221 Goodbye\r\n")
                    return
                else:
                    self.wfile.write(b"502 Unsupported command\r\n")
    with ThreadingTCPServer(("127.0.0.1", 0), LocalSMTPHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "SMTP_PORT", server.server_address[1])
        monkeypatch.setattr(settings, "SMTP_TLS_MODE", "plain")
        monkeypatch.setattr(settings, "SMTP_USERNAME", "")
        monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "accounts@example.com")
        monkeypatch.setattr(settings, "PASSWORD_RESET_URL", "https://app.example.com/")
        try:
            email, original = register(client)
            assert request_link(client, email).status_code == 202
            assert len(received) == 1
            message = received[0]
            assert message["To"] == email
            link = next(line for line in message.get_content().splitlines() if line.startswith("https://"))
            token = parse_qs(urlsplit(link).fragment)["reset_token"][0]
            assert not urlsplit(link).query
            with SessionLocal() as db:
                assert db.query(PasswordResetToken).one().token_hash == sha256(token.encode()).hexdigest()
            assert confirm(client, token).status_code == 204
            assert len(received) == 2
            assert NEW_PASSWORD not in received[1].get_content() and token not in received[1].get_content()
            assert client.post("/api/v1/auth/login", json={"email": email, "password": NEW_PASSWORD}).status_code == 200
            assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {original['access_token']}"}).status_code == 401
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("changes", [
    {"PASSWORD_RESET_URL": "http://app.example.com/"},
    {"PASSWORD_RESET_URL": "https://app.example.com/?redirect=evil"},
    {"PASSWORD_RESET_URL": "https://user:password@app.example.com/"},
    {"SMTP_TLS_MODE": "plain"},
])
def test_production_recovery_rejects_unsafe_configuration(changes):
    with pytest.raises(ValueError):
        Settings(_env_file=None, ENVIRONMENT="production", SECRET_KEY="production-test-secret-at-least-32-bytes", **changes)


def test_password_reset_migration_preserves_legacy_users(tmp_path, monkeypatch):
    backend_root = Path(__file__).parents[1]
    database_url = f"sqlite:///{(tmp_path / 'password-reset-migration.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    command.upgrade(config, "20261111_0017")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            connection.execute(text("INSERT INTO users (id, name, email, password_hash, is_active) VALUES (1, 'Legacy', 'legacy@example.com', 'old-hash', 1)"))
        command.upgrade(config, "20261111_0018")
        with migration_engine.connect() as connection:
            assert connection.execute(text("SELECT password_hash, auth_version FROM users WHERE id = 1")).one() == ("old-hash", 0)
        assert "password_reset_tokens" in inspect(migration_engine).get_table_names()
        command.downgrade(config, "20261111_0017")
        with migration_engine.connect() as connection:
            assert connection.scalar(text("SELECT password_hash FROM users WHERE id = 1")) == "old-hash"
    finally:
        migration_engine.dispose()
