import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as test_client:
        yield test_client


def test_sync_status_is_safe_and_disabled_by_default(client):
    response = client.get("/api/sync/status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["configured"] is False
    assert body["running"] is False
    assert body["interval_seconds"] == 60
    assert body["last_success_at"] is None
    assert set(("tracked", "pending", "conflicts", "synced")) <= body.keys()
    assert "password" not in response.text
    assert "publishable" not in response.text
    assert "token" not in response.text


def test_manual_sync_endpoint_uses_runner_with_automatic_mode_off(client, monkeypatch):
    from orchestrator.api.routes import sync

    async def fake_run():
        return {
            "pulled": {"applied": 2},
            "pushed": {"pushed": 3},
            "status": {"tracked": 5, "pending": 1, "conflicts": 4, "synced": 0},
        }

    monkeypatch.setattr(sync.sync_runner, "run_once", fake_run)
    response = client.post("/api/sync/run")
    assert response.status_code == 200
    assert response.json()["pulled"]["applied"] == 2
    assert response.json()["status"]["conflicts"] == 4
    assert client.get("/api/settings").json()["supabase_sync_enabled"] is False


def test_sync_settings_validation_and_local_defaults(client):
    settings = client.get("/api/settings").json()
    assert settings["supabase_sync_enabled"] is False
    assert settings["supabase_sync_interval_seconds"] == 60
    assert settings["supabase_configured"] is False
    for interval in (0, 14, 86401):
        response = client.patch(
            "/api/settings", json={"values": {"supabase_sync_interval_seconds": interval}}
        )
        assert response.status_code == 400
    response = client.patch(
        "/api/settings",
        json={"values": {"supabase_sync_enabled": True, "supabase_sync_interval_seconds": 15}},
    )
    assert response.status_code == 200
    assert response.json()["supabase_sync_enabled"] is True
    assert response.json()["supabase_sync_interval_seconds"] == 15


def test_sync_status_reports_the_stopped_reason_and_failure_streak(client):
    from orchestrator.core.db import session_scope
    from orchestrator.services import sync_service

    with session_scope() as session:
        sync_service.write_health(
            session,
            consecutive_failures=4,
            last_failure_kind="permanent",
            last_failure_error="Supabase returned HTTP 400: invalid version transition: 0 -> 3",
            stopped=True,
            stopped_reason="permanent client error: invalid version transition: 0 -> 3",
        )

    body = client.get("/api/sync/status").json()
    assert body["stopped"] is True
    assert body["consecutive_failures"] == 4
    assert body["stopped_reason"] == (
        "permanent client error: invalid version transition: 0 -> 3"
    )
    assert body["last_failure_kind"] == "permanent"


def test_manual_sync_returns_the_permanent_error_as_structured_detail(client, monkeypatch):
    from orchestrator.api.routes import sync
    from orchestrator.services.sync_transport import SyncPermanentError

    async def refuse():
        raise SyncPermanentError(
            "Supabase returned HTTP 400: invalid version transition: 0 -> 3",
            status_code=400,
            detail={
                "retryable": False,
                "recovery": "orchestrator sync reinit --dry-run",
            },
        )

    monkeypatch.setattr(sync.sync_runner, "run_once", refuse)
    response = client.post("/api/sync/run")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Supabase returned HTTP 400: invalid version transition: 0 -> 3"
    assert body["detail"]["kind"] == "permanent"
    assert body["detail"]["status_code"] == 400
    assert body["detail"]["recovery"] == "orchestrator sync reinit --dry-run"


def test_manual_sync_returns_a_transient_error_as_503(client, monkeypatch):
    """A retryable upstream failure is 503, not 400: the request itself was not wrong."""
    from orchestrator.api.routes import sync
    from orchestrator.services.sync_transport import SyncTransientError

    async def unavailable():
        raise SyncTransientError(
            "Supabase returned HTTP 503: unavailable",
            status_code=503,
            detail={"retryable": True},
        )

    monkeypatch.setattr(sync.sync_runner, "run_once", unavailable)
    response = client.post("/api/sync/run")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "Supabase returned HTTP 503: unavailable"
    assert body["detail"]["kind"] == "transient"
    assert body["detail"]["retryable"] is True
