import pytest
from pydantic import ValidationError

from app.core.config import Settings


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
