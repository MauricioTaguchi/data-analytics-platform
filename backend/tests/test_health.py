def test_liveness_and_readiness(client):
    live = client.get("/health/live", headers={"X-Request-ID": "portfolio-test"})
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.headers["X-Request-ID"] == "portfolio-test"
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json()["database"] == "available"
