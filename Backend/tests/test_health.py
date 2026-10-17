def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_when_db_up(client):
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["database"] == "up"


def test_ready_when_db_down(client):
    client.app.state.engine.dispose()
    client.app.state.session_factory = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    # A failing session factory surfaces as a handled 500 error body, not a crash.
    r = client.get("/ready")
    assert r.status_code in (500, 503)


def test_request_id_generated_and_echoed(client):
    r = client.get("/health")
    assert len(r.headers["x-request-id"]) > 0
    r2 = client.get("/health", headers={"x-request-id": "abc123"})
    assert r2.headers["x-request-id"] == "abc123"