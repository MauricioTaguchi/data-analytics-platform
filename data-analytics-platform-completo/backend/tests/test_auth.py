def test_register_and_login(client):
    register = client.post(
        "/api/v1/auth/register",
        json={
            "name": "Mauricio",
            "email": "mauricio@example.com",
            "password": "senha12345",
        },
    )
    assert register.status_code == 201
    assert register.json()["token_type"] == "bearer"

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "mauricio@example.com",
            "password": "senha12345",
        },
    )
    assert login.status_code == 200
