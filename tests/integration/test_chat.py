from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.adapters.chat.mock import MockDriver
from orchestrator.core import config as config_mod
from orchestrator.core.security import same_origin_ok
from orchestrator.services import chat_service
from tests.conftest import make_repo, requires_git

HOST = {"host": "127.0.0.1:8321"}


@pytest.fixture
def client(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as c:
        yield c


@pytest.fixture
def enabled(client):
    client.patch("/api/settings", json={"values": {"allow_interactive_sessions": True}})
    return client


@pytest.fixture
def project(enabled, tmp_path):
    repo = make_repo(tmp_path / "repo")
    return enabled.post("/api/projects", json={"name": "demo", "path": str(repo)}).json()


def start(client, project, **fields):
    body = {"project_id": project["id"], "adapter": "mock", **fields}
    r = client.post("/api/chat/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def until(ws, kind, **match):
    for _ in range(200):
        event = ws.receive_json()
        if event["type"] == kind and all(event.get(k) == v for k, v in match.items()):
            return event
    raise AssertionError(f"no {kind} event")


def wait_ready(ws):
    first = ws.receive_json()
    assert first["type"] == "history"
    if first["status"] != "idle" and not any(
        e["type"] == "status" and e["status"] == "idle" for e in first["events"]
    ):
        until(ws, "status", status="idle")


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"host": "127.0.0.1:8321"}, True),
        ({"host": "localhost:8321", "origin": "http://localhost:8321"}, True),
        ({"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}, True),
        ({"host": "127.0.0.1:8321", "origin": "https://evil.example"}, False),
        ({"host": "127.0.0.1:8321", "origin": "null"}, False),
        # DNS rebinding: Origin matches Host, and both are the attacker's domain.
        ({"host": "evil.example:8321", "origin": "http://evil.example:8321"}, False),
        ({"host": "evil.example:8321"}, False),
    ],
)
def test_same_origin(home, headers, allowed):
    assert same_origin_ok(headers) is allowed


def test_token_allows_other_hosts_but_not_other_origins(home, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "secret")
    config_mod.reset_settings_cache()
    assert same_origin_ok({"host": "192.168.1.5:8321", "origin": "http://192.168.1.5:8321"})
    assert not same_origin_ok({"host": "192.168.1.5:8321", "origin": "http://evil.example"})


def test_off_by_default(client, tmp_path):
    repo = make_repo(tmp_path / "repo")
    project = client.post("/api/projects", json={"name": "demo", "path": str(repo)}).json()
    assert client.get("/api/chat/harnesses").json()["enabled"] is False
    r = client.post("/api/chat/sessions", json={"project_id": project["id"], "adapter": "mock"})
    assert r.status_code == 403


def test_rest_refuses_other_origins(enabled):
    r = enabled.get("/api/chat/sessions", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = enabled.get("/api/chat/sessions", headers={"host": "evil.example"})
    assert r.status_code == 403


def test_websocket_refuses_other_origins(enabled, project):
    chat = start(enabled, project)
    with pytest.raises(WebSocketDisconnect):
        with enabled.websocket_connect(
            f"/ws/chat/{chat['id']}", headers={**HOST, "origin": "https://evil.example"}
        ):
            pass
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


def test_websocket_requires_the_token(enabled, project, monkeypatch):
    chat = start(enabled, project)
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "secret")
    config_mod.reset_settings_cache()
    with pytest.raises(WebSocketDisconnect):
        with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST):
            pass
    monkeypatch.delenv("ORCHESTRATOR_TOKEN")
    config_mod.reset_settings_cache()
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


def test_catalog_explains_unavailable_harnesses(enabled):
    harnesses = {h["name"]: h for h in enabled.get("/api/chat/harnesses").json()["harnesses"]}
    assert harnesses["mock"]["available"]
    assert "private protocol" in harnesses["zcode"]["reason"]
    assert "Adapters" in harnesses["claude_cli"]["reason"]
    assert harnesses["pi"]["default_permission"] == "ask"
    assert [m["id"] for m in harnesses["pi"]["permissions"] if m["dangerous"]] == ["yolo"]


def test_unknown_permission_is_rejected(enabled, project):
    r = enabled.post(
        "/api/chat/sessions",
        json={"project_id": project["id"], "adapter": "mock", "permission": "nope"},
    )
    assert r.status_code == 400


@requires_git
def test_session_flow_in_a_worktree(enabled, project, tmp_path):
    chat = start(enabled, project)
    assert chat["branch_name"].startswith("chat/")
    assert chat["cwd"] != project["path"]
    assert chat["permission"] == "ask"

    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "add a note"})
        approval = until(ws, "approval")
        assert until(ws, "status", status="awaiting_approval")
        ws.send_json({"type": "approval", "id": approval["id"], "choice": "allow"})
        assert until(ws, "tool_end")["error"] is False
        until(ws, "usage")
        until(ws, "status", status="idle")

    row = enabled.get(f"/api/chat/sessions/{chat['id']}").json()
    assert row["title"] == "add a note"
    assert row["usage"]["input_tokens"] == 100

    changes = enabled.get(f"/api/chat/sessions/{chat['id']}/changes").json()
    assert "mock-chat-1.md" in changes["uncommitted"]
    assert not (tmp_path / "repo" / "mock-chat-1.md").exists()

    # Off until merging is allowed, and the session must have ended first.
    assert enabled.post(f"/api/chat/sessions/{chat['id']}/merge").status_code == 400
    assert enabled.post(f"/api/chat/sessions/{chat['id']}/close").json()["status"] == "closed"
    assert enabled.post(f"/api/chat/sessions/{chat['id']}/merge").status_code == 400
    enabled.patch("/api/settings", json={"values": {"merge_enabled": True}})
    assert enabled.post(f"/api/chat/sessions/{chat['id']}/merge").status_code == 200
    assert (tmp_path / "repo" / "mock-chat-1.md").is_file()

    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        history = ws.receive_json()
        assert history["status"] == "closed"
        assert [e["type"] for e in history["events"] if e["type"] == "user"] == ["user"]
        assert ws.receive_json()["type"] == "end"

    assert enabled.delete(f"/api/chat/sessions/{chat['id']}").status_code == 204
    assert enabled.get(f"/api/chat/sessions/{chat['id']}").status_code == 404


@requires_git
def test_session_without_a_project(enabled):
    r = enabled.post("/api/chat/sessions", json={"adapter": "mock", "workspace": "checkout"})
    assert r.status_code == 201, r.text
    chat = r.json()
    folder = Path(chat["cwd"])
    assert chat["project_id"] is None
    assert chat["workspace"] == "folder"
    assert chat["branch_name"] is None
    assert folder.is_dir() and not list(folder.iterdir())

    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "add a note"})
        approval = until(ws, "approval")
        ws.send_json({"type": "approval", "id": approval["id"], "choice": "allow"})
        assert until(ws, "tool_end")["error"] is False
        until(ws, "status", status="idle")
    assert (folder / "mock-chat-1.md").is_file()

    changes = enabled.get(f"/api/chat/sessions/{chat['id']}/changes").json()
    assert changes["branch"] is None and changes["merge_enabled"] is False
    assert enabled.post(f"/api/chat/sessions/{chat['id']}/merge").status_code == 400

    # The session owns this folder, so it goes with the session.
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    assert enabled.delete(f"/api/chat/sessions/{chat['id']}").status_code == 204
    assert not folder.exists()


def test_session_as_an_agent(enabled, project):
    body = {"name": "helper", "instructions": "Be brief.", "harness_adapter": "mock"}
    agent = enabled.post("/api/agents", json=body).json()
    choice = next(
        a for a in enabled.get("/api/chat/harnesses").json()["agents"] if a["name"] == "helper"
    )
    assert choice["adapter"] == "mock"

    sent = []
    original = MockDriver.prompt

    async def record(self, text):
        sent.append(text)
        await original(self, text)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MockDriver, "prompt", record)
        chat = start(enabled, project, agent_id=agent["id"])
        assert chat["agent_name"] == "helper"
        with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
            wait_ready(ws)
            for text in ("first", "second"):
                ws.send_json({"type": "prompt", "text": text})
                approval = until(ws, "approval")
                ws.send_json({"type": "approval", "id": approval["id"], "choice": "deny"})
                until(ws, "status", status="idle")

    # Sent once, and never shown as something you typed.
    assert sent == ["Agent instructions (helper):\nBe brief.\n\nRequest:\nfirst", "second"]
    row = enabled.get(f"/api/chat/sessions/{chat['id']}").json()
    assert row["title"] == "first"
    typed = [e["text"] for e in chat_service.history(chat["id"]) if e["type"] == "user"]
    assert typed == ["first", "second"]

    # The name outlives the agent.
    assert enabled.delete(f"/api/agents/{agent['id']}").status_code < 300
    assert enabled.get(f"/api/chat/sessions/{chat['id']}").json()["agent_name"] == "helper"


def test_disabled_or_unknown_agent_is_rejected(enabled, project):
    body = {"name": "off", "harness_adapter": "mock", "enabled": False}
    agent = enabled.post("/api/agents", json=body).json()
    enabled.patch(f"/api/agents/{agent['id']}", json={"enabled": False})
    names = [a["name"] for a in enabled.get("/api/chat/harnesses").json()["agents"]]
    assert "off" not in names
    payload = {"project_id": project["id"], "adapter": "mock"}
    r = enabled.post("/api/chat/sessions", json={**payload, "agent_id": agent["id"]})
    assert r.status_code == 400
    r = enabled.post("/api/chat/sessions", json={**payload, "agent_id": 999})
    assert r.status_code == 404


def test_claude_offers_every_model_even_without_a_profile(enabled):
    claude = next(
        h
        for h in enabled.get("/api/chat/harnesses").json()["harnesses"]
        if h["kind"] == "claude_cli"
    )
    assert {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"} <= set(
        claude["models"]
    )


def test_unknown_project_is_rejected(enabled):
    r = enabled.post("/api/chat/sessions", json={"project_id": 999, "adapter": "mock"})
    assert r.status_code == 404


def test_denied_tool_writes_nothing(enabled, project):
    chat = start(enabled, project)
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "no"})
        approval = until(ws, "approval")
        ws.send_json({"type": "approval", "id": approval["id"], "choice": "deny"})
        assert until(ws, "tool_end")["error"] is True
    changes = enabled.get(f"/api/chat/sessions/{chat['id']}/changes").json()
    assert changes["uncommitted"] == ""
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


@requires_git
def test_transcript_is_redacted_across_streamed_chunks(enabled, project):
    chat = start(enabled, project, permission="yolo")
    secret = "sk-ant-" + "a" * 40
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": f"token {secret}"})
        until(ws, "status", status="idle")
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    text = chat_service._transcript(chat["id"]).read_text()
    assert secret not in text
    assert "mock-chat-1.md" in text


@requires_git
def test_prompt_during_a_turn_is_queued_when_the_harness_cannot_steer(enabled, project):
    chat = start(enabled, project)
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "one"})
        first = until(ws, "approval")
        ws.send_json({"type": "prompt", "text": "two"})
        assert "Queued" in until(ws, "notice")["message"]
        ws.send_json({"type": "approval", "id": first["id"], "choice": "allow"})
        second = until(ws, "approval")
        ws.send_json({"type": "approval", "id": second["id"], "choice": "allow"})
        until(ws, "text")
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


@requires_git
def test_live_control_changes_are_saved(enabled, project):
    chat = start(enabled, project)
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "set_effort", "value": "high"})
        until(ws, "config", effort="high")
        ws.send_json({"type": "set_model", "value": "other"})
        assert "cache" in until(ws, "notice")["message"]
        ws.send_json({"type": "bogus"})
        assert "unknown message" in until(ws, "error")["message"]
    row = enabled.get(f"/api/chat/sessions/{chat['id']}").json()
    assert (row["effort"], row["model"]) == ("high", "other")
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


@requires_git
def test_session_limit(enabled, project):
    chats = [start(enabled, project) for _ in range(chat_service.MAX_LIVE_SESSIONS)]
    r = enabled.post("/api/chat/sessions", json={"project_id": project["id"], "adapter": "mock"})
    assert r.status_code == 400
    for chat in chats:
        enabled.post(f"/api/chat/sessions/{chat['id']}/close")


@requires_git
def test_restart_marks_open_sessions_interrupted(enabled, project):
    from orchestrator.core.db import session_scope
    from orchestrator.core.models import ChatSession

    chat = start(enabled, project)
    time.sleep(0.1)
    chat_service.manager.live.pop(chat["id"])
    with session_scope() as s:
        chat_service.recover(s)
    assert enabled.get(f"/api/chat/sessions/{chat['id']}").json()["status"] == "interrupted"
    with session_scope() as s:
        assert json.dumps(s.get(ChatSession, chat["id"]).usage) == "{}"
