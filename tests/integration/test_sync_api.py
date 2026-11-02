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
