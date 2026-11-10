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
    assert r.json()["db_revision"] == "0010_notifications"


def test_startup_upgrades_existing_database(initialized):
    from orchestrator.api.main import create_app
    from orchestrator.core import db

    db.dispose_engines()
    db.downgrade(revision="0001_baseline")
    assert db.current_revision() == "0001_baseline"

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as c:
        assert c.get("/api/health").json()["db_revision"] == "0010_notifications"
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


def test_memory_budget_and_triage_endpoints_are_read_only(client, tmp_path):
    repo = tmp_path / "demo-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "build-loop.md").write_text("# loop\n")
    r = client.post("/api/projects", json={"name": "demo", "path": str(repo)})
    assert r.status_code == 201, r.text
    client.post(
        "/api/memory",
        json={
            "category": "project",
            "scope": "gotchas",
            "project_slug": "demo",
            "content": "T-9 decisions: (1) the gate blocks when an artifact is missing.",
        },
    )
    budget = client.get("/api/memory/budget", params={"project": "demo"})
    assert budget.status_code == 200
    body = budget.json()
    assert set(body["budgets"]) == {"global", "role", "project"}
    assert set(body["roles"]) == {"architect", "worker", "supervisor"}
    assert body["pending_count"] == 1

    tri = client.get("/api/memory/triage", params={"status": "pending", "project": "demo"})
    assert tri.status_code == 200
    row = tri.json()["rows"][0]
    assert row["suggested_action"] == "rewrite-durable"
    codes = {f["code"] for f in row["flags"]}
    assert "task_shape" in codes and "unknown-project" not in codes
    # Triage reports, it never decides: the row stays pending and unapproved.
    pending = client.get("/api/memory?status=pending").json()
    assert [e["id"] for e in pending] == [row["id"]]
    assert all(e["approved"] is False for e in pending)
    assert row["id"] not in [e["id"] for e in client.get("/api/memory?status=approved").json()]
    assert client.get("/api/memory/triage", params={"status": "bogus"}).status_code == 422


def test_approval_eviction_is_refused_and_override_is_explicit(client, monkeypatch):
    """DoD 7c: the response names the casualty, and only an explicit override approves it."""
    from orchestrator.core import config as config_mod
    from orchestrator.core.db import session_scope
    from orchestrator.core.schemas import MemoryCreate
    from orchestrator.services import memory_service
    from orchestrator.utils.truncate import estimate_tokens

    def tokens(entry):
        return estimate_tokens(f"- {entry.content.strip()}\n")

    with session_scope() as s:
        keep = memory_service.create(
            s,
            MemoryCreate(
                category="project", scope="gotchas", project_slug="demo",
                content="Keep rule " + "alpha " * 10, approved=True,
            ),
        )
        risk = memory_service.create(
            s,
            MemoryCreate(
                category="project", scope="gotchas", project_slug="demo",
                content="At-risk rule " + "beta " * 10, approved=True,
            ),
        )
        pending = memory_service.create(
            s,
            MemoryCreate(
                category="project", scope="gotchas", project_slug="demo",
                content="New rule " + "gamma " * 10, approved=False,
            ),
        )
        cap = tokens(keep) + tokens(risk)
        pending_id = pending.id
    monkeypatch.setenv("ORCHESTRATOR_MAX_MEMORY_TOKENS_PROJECT", str(cap))
    config_mod.reset_settings_cache()
    with session_scope() as s:
        casualty = memory_service.approval_impact(s, pending_id)["evicted"][0]["id"]

    refused = client.post(f"/api/memory/{pending_id}/approve")
    assert refused.status_code == 400
    body = refused.json()
    assert f"#{casualty} (" in body["error"]
    # DoD 7c/finding 4: the casualty list is STRUCTURE in the frozen error body, so a GUI
    # never has to match a substring of the prose to offer the override.
    assert [row["id"] for row in body["detail"]["evicted"]] == [casualty]
    assert body["detail"]["evicted"][0]["bucket"] == "project"
    approved = client.post(f"/api/memory/{pending_id}/approve", params={"override": "true"})
    assert approved.status_code == 200 and approved.json()["approved"] is True


def test_creating_an_approved_row_over_the_cap_is_refused_and_override_is_explicit(
    client, monkeypatch
):
    """DoD 7c also covers `POST /api/memory {approved: true}`, not only the approve route."""
    from orchestrator.core import config as config_mod
    from orchestrator.core.db import session_scope
    from orchestrator.core.schemas import MemoryCreate
    from orchestrator.services import memory_service
    from orchestrator.utils.truncate import estimate_tokens

    def tokens(content):
        return estimate_tokens(f"- {content}")

    with session_scope() as s:
        keep = memory_service.create(
            s,
            MemoryCreate(
                category="project", scope="gotchas", project_slug="demo",
                content="Keep rule " + "alpha " * 10, approved=True,
            ),
        )
        keep_id = keep.id
        cap = tokens(keep.content)
    monkeypatch.setenv("ORCHESTRATOR_MAX_MEMORY_TOKENS_PROJECT", str(cap))
    config_mod.reset_settings_cache()

    body = {
        "category": "project",
        "scope": "gotchas",
        "project_slug": "demo",
        "content": "New approved rule " + "gamma " * 10 + " that is far too long for the cap.",
        "approved": True,
    }
    refused = client.post("/api/memory", json=body)
    assert refused.status_code == 400
    body_json = refused.json()
    assert f"#{keep_id} (" in body_json["error"]
    assert [row["id"] for row in body_json["detail"]["evicted"]] == [keep_id]

    accepted = client.post("/api/memory", params={"override": "true"}, json=body)
    assert accepted.status_code == 201 and accepted.json()["approved"] is True


def test_triage_flags_an_unregistered_project(client):
    client.post(
        "/api/memory",
        json={
            "category": "project",
            "scope": "gotchas",
            "project_slug": "ghost",
            "content": "The gate blocks when an artifact is missing.",
        },
    )
    row = client.get("/api/memory/triage", params={"project": "ghost"}).json()["rows"][0]
    assert {f["code"] for f in row["flags"]} == {"unknown-project"}
    assert row["suggested_action"] == "fix-stale"


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
    # T-9: notifications are off on a new install, but the human-must-act triggers are
    # subscribed so enabling the master does useful work; noisy machinery stays silent.
    assert s["notifications_enabled"] is False
    assert s["notification_run_completed"] is True
    assert s["notification_worktree_created"] is False
    assert client.patch(
        "/api/settings", json={"values": {"notifications_enabled": True}}
    ).json()["notifications_enabled"] is True
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


@requires_git
def test_project_doctor_route_stays_static(client, tmp_path):
    """T-6: `--run-commands` is a CLI flag only. The REST doctor route keeps its frozen shape
    ({ok, checks:[{name, ok, detail}]}) and executes nothing, so the GUI can never trigger a
    project command — the probe that does run one is reachable from the terminal, deliberately
    and only there."""
    from orchestrator.services import git_service
    from tests.conftest import make_repo

    repo = make_repo(tmp_path / "api-doctor")
    p = client.post(
        "/api/projects",
        json={"name": "ad", "path": str(repo), "test_command": "scripts/orch_verify.sh test"},
    ).json()
    r = client.get(f"/api/projects/{p['id']}/doctor")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "checks"}
    assert body["checks"] and all(set(c) == {"name", "ok", "detail"} for c in body["checks"])
    # the opt-in probe is absent from the route's output ... and it created no worktree
    assert not [c for c in body["checks"] if "worktree" in c["name"]]
    assert git_service.managed_worktrees() == []
    # the route takes no extra query parameter: shape unchanged
    assert client.get(f"/api/projects/{p['id']}/doctor?run_commands=true").json() == body


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


def test_editing_an_approved_row_over_the_cap_returns_the_casualty_in_detail(client, monkeypatch):
    """Round-4 F4: the PATCH edit path is gated too, with the same structured 400 body."""
    from orchestrator.core import config as config_mod
    from orchestrator.core.db import session_scope
    from orchestrator.core.schemas import MemoryCreate
    from orchestrator.services import memory_service
    from orchestrator.utils.truncate import estimate_tokens

    def tokens(entry):
        return estimate_tokens(f"- {entry.content.strip()}\n")

    with session_scope() as s:
        keep = memory_service.create(
            s,
            MemoryCreate(
                category="project", scope="gotchas", project_slug="demo",
                content="Keep rule " + "alpha " * 10, approved=True,
            ),
        )
        edited = memory_service.create(
            s,
            MemoryCreate(
                category="project", scope="gotchas", project_slug="demo",
                content="Edited rule " + "beta " * 10, approved=True,
            ),
        )
        cap = tokens(keep) + tokens(edited)
        edited_id, keep_id = edited.id, keep.id
    monkeypatch.setenv("ORCHESTRATOR_MAX_MEMORY_TOKENS_PROJECT", str(cap))
    config_mod.reset_settings_cache()

    longer = "Edited rule " + "beta " * 30
    refused = client.patch(f"/api/memory/{edited_id}", json={"content": longer})
    assert refused.status_code == 400
    body = refused.json()
    assert set(body) == {"error", "detail"}
    assert body["detail"]["action"] == f"editing #{edited_id}"
    casualties = {row["id"]: row for row in body["detail"]["evicted"]}
    assert keep_id in casualties
    # The longer edit can also push the edited row itself out; both are named, and the
    # self-casualty is flagged as structure.
    assert casualties[edited_id]["self"] is True
    assert client.get("/api/memory?scope=gotchas").json()[0]["content"] != longer

    ok = client.patch(
        f"/api/memory/{edited_id}", params={"override": "true"}, json={"content": longer}
    )
    assert ok.status_code == 200
    assert ok.json()["content"] == longer.strip()
