from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models.password_reset import PasswordResetToken
from app.models.password_reset_delivery import PasswordResetDelivery
from app.models.user import User
from app.services import password_reset_service as recovery
from app.services import recovery_delivery_service as delivery

PASSWORD = "OriginalPassword123"


@pytest.fixture
def mailer(monkeypatch):
    messages = []
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "accounts@example.com")
    monkeypatch.setattr(settings, "PASSWORD_RESET_URL", "https://app.example.com/")
    monkeypatch.setattr(recovery, "deliver_reset_link", lambda email, token: messages.append(("reset", email, token)))
    monkeypatch.setattr(recovery, "notify_password_changed", lambda email: messages.append(("changed", email, None)))
    return messages


def register(client):
    email = f"durable-{uuid4().hex}@example.com"
    response = client.post("/api/v1/auth/register", json={"name": "Delivery user", "email": email, "password": PASSWORD})
    assert response.status_code == 201
    return email


def request(client, email):
    response = client.post("/api/v1/auth/password-reset/request", json={"email": email})
    assert response.status_code == 202
    return response


def make_due():
    with SessionLocal() as db:
        db.query(PasswordResetDelivery).filter_by(state="pending").update(
            {PasswordResetDelivery.next_attempt_at: datetime.now(timezone.utc) - timedelta(seconds=1)},
        )
        db.commit()


def test_uniform_durable_admission_and_delivery_after_new_database_connection(client, mailer):
    email = register(client)
    unknown = f"missing-{uuid4().hex}@example.com"
    assert request(client, email).json() == request(client, unknown).json()
    assert not mailer
    with SessionLocal() as db:
        rows = db.query(PasswordResetDelivery).all()
        assert len(rows) == 2
        assert db.query(PasswordResetToken).count() == 0
        for row in rows:
            assert row.user_id is None and row.kind == "reset" and row.state == "pending"
            assert email not in row.encrypted_payload and unknown not in row.encrypted_payload
    engine.dispose()
    assert delivery.dispatch_recovery_mail()
    assert delivery.dispatch_recovery_mail()
    assert len(mailer) == 1 and mailer[0][1] == email
    with SessionLocal() as db:
        assert {row.state for row in db.query(PasswordResetDelivery)} == {"sent", "cancelled"}
        assert all(row.encrypted_payload is None for row in db.query(PasswordResetDelivery))
        assert db.query(PasswordResetToken).one().token_hash == sha256(mailer[0][2].encode()).hexdigest()
    assert not delivery.dispatch_recovery_mail()


def test_failed_smtp_retries_the_same_encrypted_single_use_link(client, mailer, monkeypatch, caplog):
    email = register(client)
    request(client, email)
    attempts = []
    def unavailable(recipient, token):
        attempts.append((recipient, token))
        raise OSError(f"secret {recipient} {token}")
    monkeypatch.setattr(recovery, "deliver_reset_link", unavailable)
    assert delivery.dispatch_recovery_mail()
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        token = db.query(PasswordResetToken).one()
        assert row.state == "pending" and row.attempts == 1
        assert row.lease_id is None and row.lease_until is None
        assert token.delivered_at is None and token.used_at is None
        assert attempts[0][1] not in row.encrypted_payload and email not in row.encrypted_payload
    assert email not in caplog.text and attempts[0][1] not in caplog.text
    assert not delivery.dispatch_recovery_mail()  # Backoff is enforced.
    make_due()
    monkeypatch.setattr(recovery, "deliver_reset_link", lambda email, token: attempts.append((email, token)))
    assert delivery.dispatch_recovery_mail()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        assert row.state == "sent" and row.attempts == 2 and row.encrypted_payload is None
    response = client.post("/api/v1/auth/password-reset/confirm", json={"token": attempts[0][1], "new_password": "ReplacementPassword456"})
    assert response.status_code == 204
    with SessionLocal() as db:
        assert db.query(PasswordResetDelivery).filter_by(kind="changed", state="pending").count() == 1
    assert delivery.dispatch_recovery_mail()
    assert mailer == [("changed", email, None)]


def test_retry_limit_erases_payload_and_invalidates_token(client, mailer, monkeypatch):
    email = register(client)
    request(client, email)
    monkeypatch.setattr(recovery, "deliver_reset_link", lambda *_: (_ for _ in ()).throw(OSError("unavailable")))
    for _ in range(delivery.MAX_ATTEMPTS):
        make_due()
        assert delivery.dispatch_recovery_mail()
    assert not delivery.dispatch_recovery_mail()
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        assert row.state == "failed" and row.attempts == delivery.MAX_ATTEMPTS
        assert row.encrypted_payload is None and row.lease_id is None
        assert db.query(PasswordResetToken).one().used_at is not None


def test_expired_requests_are_scrubbed_without_smtp_even_when_delivery_is_disabled(client, mailer, monkeypatch):
    email = register(client)
    request(client, email)
    with SessionLocal() as db:
        db.query(PasswordResetDelivery).update({PasswordResetDelivery.expires_at: datetime.now(timezone.utc) - timedelta(seconds=1)})
        db.commit()
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    delivery.purge_recovery_delivery_history()
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        assert row.state == "failed" and row.encrypted_payload is None
        assert db.query(PasswordResetToken).count() == 0
        row.completed_at = datetime.now(timezone.utc) - timedelta(days=delivery.TERMINAL_RETENTION_DAYS + 1)
        db.commit()
    assert delivery.purge_recovery_delivery_history() == 1
    assert not mailer


def test_key_rotation_fails_closed_and_scrubs_old_payload(client, mailer, monkeypatch):
    request(client, register(client))
    monkeypatch.setattr(settings, "SECRET_KEY", "rotated-secret-key-at-least-32-bytes")
    assert delivery.dispatch_recovery_mail()
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        assert row.state == "failed" and row.encrypted_payload is None
        assert db.query(PasswordResetToken).count() == 0
    assert not mailer


def test_abandoned_claim_recovers_same_token_and_old_receipt_cannot_finish_new_lease(client, mailer):
    email = register(client)
    request(client, email)
    first = delivery._claim()
    prepared = delivery._prepare(*first)
    assert prepared[1] == email and prepared[2]
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    second = delivery._claim()
    assert second[0] == first[0] and second[1] != first[1]
    assert delivery._prepare(*second) == prepared
    delivery._finish(*first, accepted=True)
    with SessionLocal() as db:
        assert db.query(PasswordResetDelivery).one().lease_id == second[1]
        assert db.query(PasswordResetToken).one().delivered_at is None
    recovery.deliver_reset_link(prepared[1], prepared[2])
    delivery._finish(*second, accepted=True)
    assert mailer == [("reset", email, prepared[2])]
    assert not delivery.dispatch_recovery_mail()


def test_two_dispatchers_cannot_claim_one_pending_message(client, mailer):
    request(client, register(client))
    barrier = Barrier(2)
    def claim(_):
        barrier.wait(timeout=5)
        return delivery._claim()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("prepared", [False, True])
def test_closure_scrubs_unresolved_and_prepared_deliveries(client, mailer, prepared):
    email = register(client)
    request(client, email)
    claim = delivery._claim() if prepared else None
    if claim:
        assert delivery._prepare(*claim)
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=email).one()
        db.query(User).filter_by(id=user.id).update({User.id: User.id})
        user.is_active = False
        user.auth_version += 1
        delivery.cancel_account_deliveries(db, user)
        db.commit()
    if claim:
        delivery._finish(*claim, accepted=True)
    assert not delivery.dispatch_recovery_mail()
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        assert row.state == "cancelled" and row.encrypted_payload is None
        assert all(token.delivered_at is None for token in db.query(PasswordResetToken))
    assert not mailer


def test_changed_account_version_cancels_a_retry(client, mailer):
    email = register(client)
    request(client, email)
    claim = delivery._claim()
    assert delivery._prepare(*claim)
    delivery._finish(*claim, accepted=False)
    with SessionLocal() as db:
        db.query(User).filter_by(email=email).update({User.auth_version: User.auth_version + 1})
        db.commit()
    make_due()
    assert delivery.dispatch_recovery_mail()
    with SessionLocal() as db:
        assert db.query(PasswordResetDelivery).one().state == "cancelled"
        assert db.query(PasswordResetToken).one().used_at is not None
    assert not mailer


def test_database_admission_failure_is_not_acknowledged_as_accepted(client, mailer, monkeypatch):
    from sqlalchemy.exc import OperationalError
    def unavailable(*_):
        raise OperationalError("sensitive sql", {}, Exception("private"))
    enqueue = recovery.enqueue_reset_request
    monkeypatch.setattr(recovery, "enqueue_reset_request", unavailable)
    response = client.post("/api/v1/auth/password-reset/request", json={"email": "unavailable@example.com"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Password recovery is temporarily unavailable."}
    assert not mailer
    monkeypatch.setattr(recovery, "enqueue_reset_request", enqueue)
    retried = client.post("/api/v1/auth/password-reset/request", json={"email": "unavailable@example.com"})
    assert retried.status_code == 202
    with SessionLocal() as db:
        assert db.query(PasswordResetDelivery).filter_by(state="pending").count() == 1


def test_notification_and_password_change_commit_atomically(client, mailer, monkeypatch):
    email = register(client)
    request(client, email)
    assert delivery.dispatch_recovery_mail()
    token = mailer[0][2]
    def queue_failure(*_):
        raise RuntimeError("queue unavailable")
    monkeypatch.setattr(recovery, "enqueue_password_changed", queue_failure)
    with SessionLocal() as db:
        with pytest.raises(RuntimeError, match="queue unavailable"):
            recovery.confirm_password_reset(db, token, "ReplacementPassword456")
        db.rollback()
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    with SessionLocal() as db:
        assert db.query(User).filter_by(email=email).one().auth_version == 0
        assert db.query(PasswordResetToken).one().used_at is None


def test_token_and_deliverable_roll_back_together_when_encryption_fails(client, mailer, monkeypatch):
    request(client, register(client))
    claim = delivery._claim()
    encrypt = delivery._encrypt
    def broken_encrypt(_):
        raise RuntimeError("temporary encryption failure")
    monkeypatch.setattr(delivery, "_encrypt", broken_encrypt)
    with pytest.raises(RuntimeError, match="temporary encryption failure"):
        delivery._prepare(*claim)
    with SessionLocal() as db:
        assert db.query(PasswordResetToken).count() == 0
        row = db.query(PasswordResetDelivery).one()
        assert row.token_id is None and row.state == "sending"
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    monkeypatch.setattr(delivery, "_encrypt", encrypt)
    assert delivery.dispatch_recovery_mail()
    assert len(mailer) == 1


def test_stale_preparer_cannot_repopulate_a_scrubbed_or_reclaimed_delivery(client, mailer):
    request(client, register(client))
    first = delivery._claim()
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert delivery._prepare(*first) is None
    second = delivery._claim()
    assert second[1] != first[1]
    assert delivery._prepare(*first) is None
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    delivery.purge_recovery_delivery_history()
    assert delivery._prepare(*second) is None
    with SessionLocal() as db:
        row = db.query(PasswordResetDelivery).one()
        assert row.state == "failed" and row.encrypted_payload is None and row.token_id is None
        assert db.query(PasswordResetToken).count() == 0
    assert not mailer


def test_recovery_delivery_migration_preserves_existing_accounts(tmp_path, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text
    backend = Path(__file__).parents[1]
    database_url = f"sqlite:///{tmp_path / 'recovery-mail-migration.db'}"
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(config, "20261111_0019")
    migrated = create_engine(database_url)
    try:
        with migrated.begin() as connection:
            connection.execute(text("INSERT INTO users (id,name,email,password_hash,is_active) VALUES (1,'Legacy','legacy@example.com','old-hash',1)"))
        command.upgrade(config, "20261111_0020")
        schema = inspect(migrated)
        assert "password_reset_deliveries" in schema.get_table_names()
        assert {item["name"] for item in schema.get_indexes("password_reset_deliveries")} == {
            "ix_password_reset_deliveries_recipient_key", "ix_password_reset_deliveries_due",
        }
        assert {item["referred_table"] for item in schema.get_foreign_keys("password_reset_deliveries")} == {"users", "password_reset_tokens"}
        command.downgrade(config, "20261111_0019")
        assert "password_reset_deliveries" not in inspect(migrated).get_table_names()
        with migrated.connect() as connection:
            assert connection.scalar(text("SELECT password_hash FROM users WHERE id=1")) == "old-hash"
    finally:
        migrated.dispose()
