import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import requires_git


@pytest.fixture
def client(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["db_revision"] == "0009_chat_continue"


def test_startup_upgrades_existing_database(initialized):
    from orchestrator.api.main import create_app
    from orchestrator.core import db

    db.dispose_engines()
    db.downgrade(revision="0001_baseline")
    assert db.current_revision() == "0001_baseline"

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as c:
        assert c.get("/api/health").json()["db_revision"] == "0009_chat_continue"
        assert c.get("/api/dashboard").status_code == 200


def test_task_creation_and_validation(client):
    r = client.post("/api/tasks", json={"title": "Do thing", "tags": ["Mock", " qwen "]})
    assert r.status_code == 201
    body = r.json()
    assert body["short_id"].startswith("T-") and body["status"] == "planned"
    assert body["tags"] == ["mock", "qwen"]
    assert client.post("/api/tasks", json={"title": ""}).status_code == 422
    assert client.post("/api/tasks", json={"title": "x", "project_id": 999}).status_code == 404
    r = client.get(f"/api/tasks/{body['id']}")
    assert r.status_code == 200 and r.json()["runs"] == []
    assert client.get("/api/tasks/9999").status_code == 404
    # illegal transition -> 409 structured error
    r = client.post(f"/api/tasks/{body['id']}/unblock")
    assert r.status_code == 409 and "illegal task transition" in r.json()["error"]


def test_dry_run_does_not_execute(client):
    t = client.post("/api/tasks", json={"title": "x", "tags": ["mock"]}).json()
    r = client.get(f"/api/tasks/{t['id']}/dry-run")
    assert r.status_code == 200 and r.json()["provider_profile"] == "mock-worker"
    assert client.get(f"/api/runs?task_id={t['id']}").json() == []


def test_artifact_path_traversal_blocked(client, initialized):
    from orchestrator.core.db import session_scope
    from orchestrator.core.models import Run, Task

    with session_scope() as s:
        task = Task(short_id="T-x", title="t")
        s.add(task)
        s.commit()
        run = Run(short_id="R-x", task_id=task.id)
        s.add(run)
        s.commit()
        rid = run.id
    (initialized / "secret.txt").write_text("top secret")
    from orchestrator.services import artifact_service

    artifact_service.write_text(rid, "handoff.md", "ok")
    assert client.get(f"/api/artifacts/{rid}/handoff.md").text == "ok"
    for bad in ("..%2F..%2Fsecret.txt", "%2E%2E/%2E%2E/secret.txt", "..%2F..%2Forchestrator.db"):
        r = client.get(f"/api/artifacts/{rid}/{bad}")
        assert r.status_code in (400, 404) and "top secret" not in r.text
    assert client.get("/api/artifacts/99999/handoff.md").status_code == 404


def test_memory_endpoints_flow(client):
    r = client.post(
        "/api/memory",
        json={
            "category": "project",
            "scope": "gotchas",
            "project_slug": "demo",
            "content": "Watch out.",
        },
    )
    assert r.status_code == 201 and r.json()["approved"] is False
    mid = r.json()["id"]
    assert any(e["id"] == mid for e in client.get("/api/memory?status=pending").json())
    assert client.post(f"/api/memory/{mid}/approve").json()["approved"] is True
    assert (
        client.patch(f"/api/memory/{mid}", json={"content": "Watch out!"}).json()["content"]
        == "Watch out!"
    )
    assert client.get("/api/memory/search", params={"q": "Watch"}).json()[0]["id"] == mid
    export = client.get("/api/memory/export").json()
    assert client.post("/api/memory/import", json=export).json()["imported"] == 0
    assert client.post(f"/api/memory/{mid}/reject").json()["rejected"] is True
    assert (
        client.post(
            "/api/memory", json={"category": "project", "scope": "x", "content": "no slug"}
        ).status_code
        == 400
    )


def test_prompt_preview_endpoint_detects_bad_order(client):
    r = client.post(
        "/api/prompt-preview",
        json={
            "sections": [
                {"name": "dynamic_state", "content": "diff", "static": False},
                {"name": "system", "content": "role", "static": True},
            ]
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is False
    r = client.get("/api/prompt-preview?role=architect")
    assert r.json()["sections"][0]["name"] == "system" and r.json()["ok"] is True


def test_providers_never_expose_secrets(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-supersecretvalue123456")
    body = client.get("/api/providers").text
    assert "supersecretvalue" not in body and '"api_key_set":true' in body.replace(" ", "")


def test_settings_merge_gate_defaults_and_validation(client):
    s = client.get("/api/settings").json()
    assert s["merge_enabled"] is False and s["auto_merge_enabled"] is False
    assert s["supervisor_microfixes_enabled"] is False
    assert (
        client.patch("/api/settings", json={"values": {"merge_enabled": "yes"}}).status_code == 400
    )
    assert client.patch("/api/settings", json={"values": {"bogus": 1}}).status_code == 404


def test_gui_commands_disabled_by_default(client, tmp_path):
    (tmp_path / "pp").mkdir()
    p = client.post(
        "/api/projects", json={"name": "pp", "path": str(tmp_path / "pp"), "test_command": "true"}
    ).json()
    assert client.post(f"/api/projects/{p['id']}/commands/test").status_code == 403


def test_token_auth(initialized, monkeypatch):
    from orchestrator.api.main import create_app
    from orchestrator.core.config import reset_settings_cache

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "local-test-token")
    reset_settings_cache()
    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as c:
        assert c.get("/api/health").status_code == 200  # liveness stays open
        assert c.get("/api/tasks").status_code == 401
        assert c.get("/api/tasks", headers={"X-Orchestrator-Token": "wrong"}).status_code == 401
        assert (
            c.get("/api/tasks", headers={"X-Orchestrator-Token": "local-test-token"}).status_code
            == 200
        )


@requires_git
def test_websocket_streams_run_logs(client, initialized):
    import asyncio

    from orchestrator.core.db import session_scope
    from orchestrator.services import demo_service

    with session_scope() as s:
        result = asyncio.run(demo_service.run_demo(s))
    with client.websocket_connect(
        f"/ws/runs/{result['run_id']}/logs", headers={"host": "127.0.0.1:8321"}
    ) as ws:
        events = []
        while True:
            msg = ws.receive_json()
            events.append(msg)
            if msg["type"] == "end":
                break
    assert any(e["type"] == "log" and "mock-harness" in e["line"] for e in events)
    assert events[-1] == {"type": "end", "run_id": result["run_id"], "status": "completed"}


def test_fallback_ui_served(client, monkeypatch):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert r.headers["cache-control"] == "no-cache"  # or a new build stays hidden
    assert client.get("/tasks/1").status_code == 200  # SPA route
    assert client.get("/api/nope").status_code == 404


def test_agents_api(client):
    r = client.post("/api/agents", json={"name": "helper", "model_profile": "mock-agent"})
    assert r.status_code == 201
    aid = r.json()["id"]
    assert client.post("/api/agents", json={"name": "helper"}).status_code == 400
    r = client.post(f"/api/agents/{aid}/run", json={"request": "say hi", "queue": False})
    assert r.status_code == 201 and r.json()["agent_id"] == aid
    body = client.get(f"/api/agents/{aid}").json()
    assert body["routing"]["profile"] == "mock-agent" and len(body["recent_tasks"]) == 1
    s = client.post(
        "/api/schedules",
        json={
            "name": "hourly",
            "cron_expression": "0 * * * *",
            "agent_id": aid,
            "task_template_json": {"description": "ping"},
        },
    ).json()
    assert client.delete(f"/api/agents/{aid}").status_code == 400
    assert client.delete(f"/api/schedules/{s['id']}").status_code == 204
    assert client.delete(f"/api/agents/{aid}").status_code == 204
    v = client.post("/api/schedules/validate", json={"cron_expression": "*/5 * * * *"}).json()
    assert v["valid"] and v["warning"]


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"origin": "http://[bad"},
        {"host": "evil.example"},
        {"host": "evil.example", "origin": "http://evil.example"},
    ],
)
@pytest.mark.parametrize("path", ["/api/health", "/api/settings", "/api/adapters", "/"])
def test_other_websites_are_refused_everywhere(client, path, headers):
    r = client.get(path, headers=headers)
    assert r.status_code == 403
    assert r.json() == {"error": "cross-origin request refused", "detail": None}


def test_refused_requests_change_nothing(client):
    before = client.get("/api/adapters").json()
    r = client.patch(
        "/api/adapters/pi",
        json={"command": "/tmp/evil"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert client.get("/api/adapters").json() == before


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "http://127.0.0.1:8321"},
        {"host": "localhost:8321", "origin": "http://localhost:8321"},
        {"host": "[::1]:8321", "origin": "http://[::1]:8321"},
        {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"},  # the Vite dev proxy
    ],
)
def test_local_clients_are_allowed(client, headers):
    assert client.get("/api/settings", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "127.0.0.1:8321", "origin": "https://evil.example"},
        {"host": "evil.example", "origin": "http://evil.example"},
    ],
)
def test_run_log_socket_refuses_other_origins(client, headers):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws/runs/1/logs", headers=headers):
            pass
    assert e.value.code == 4403


def test_a_token_replaces_the_local_host_check(initialized, monkeypatch):
    from orchestrator.api.main import create_app
    from orchestrator.core.config import reset_settings_cache

    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "local-test-token")
    reset_settings_cache()
    with TestClient(create_app(), base_url="http://192.168.1.5:8321") as c:
        token = {"X-Orchestrator-Token": "local-test-token"}
        assert c.get("/api/settings", headers=token).status_code == 200
        assert c.get("/api/settings").status_code == 401
        refused = {**token, "origin": "https://evil.example"}
        assert c.get("/api/settings", headers=refused).status_code == 403


def test_one_time_schedule_api(client):
    body = {
        "run_at": "2099-01-02T09:00",
        "timezone": "Asia/Kuala_Lumpur",
        "task_template_json": {"title": "t"},
    }
    v = client.post("/api/schedules/validate", json=body).json()
    assert v["valid"] and v["next_runs"] == ["2099-01-02T01:00:00+00:00"]
    r = client.post("/api/schedules", json={"name": "once", **body})
    assert r.status_code == 201
    assert r.json()["run_at"].startswith("2099-01-02T01:00")
    assert r.json()["cron_expression"] == ""
    past = {**body, "run_at": "2020-01-02T09:00"}
    assert client.post("/api/schedules/validate", json=past).json()["valid"] is False
    assert client.post("/api/schedules", json={"name": "past", **past}).status_code == 400
