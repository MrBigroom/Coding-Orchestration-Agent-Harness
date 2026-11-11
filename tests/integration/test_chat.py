from __future__ import annotations

import base64
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
from orchestrator.utils.git import run_git
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


def _chat_models(client, harness):
    catalog = client.get("/api/chat/harnesses").json()["harnesses"]
    return next(h for h in catalog if h["name"] == harness)["models"]


def test_qwen4_max_preview_is_offered_by_qwen_harnesses(enabled):
    assert "qwen4-max-preview" in _chat_models(enabled, "pi")
    assert "qwen4-max-preview" in _chat_models(enabled, "qwen_code")
    assert "qwen4-max-preview" not in _chat_models(enabled, "claude_cli")


def test_a_model_added_on_the_models_page_shows_in_chat(enabled):
    providers = {p["name"]: p["id"] for p in enabled.get("/api/providers").json()}
    r = enabled.post(
        "/api/model-profiles",
        json={
            "name": "worker-kimi-k3",
            "role": "worker",
            "provider_id": providers["qwen-token-plan"],
            "model_name": "kimi-k3",
        },
    )
    assert r.status_code == 201 and r.json()["chat_harnesses"] == ["pi", "opencode", "aider"]
    assert "kimi-k3" in _chat_models(enabled, "pi")


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
    profiles = enabled.get("/api/model-profiles").json()
    mock = next(p for p in profiles if p["model_name"] == "mock-worker")
    assert mock["builtin_context_window"] is None
    enabled.patch(f"/api/model-profiles/{mock['id']}", json={"context_window": 8000})
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
        usage = until(ws, "usage")
        assert (usage["context_tokens"], usage["context_window"]) == (100, 8000)
        until(ws, "status", status="idle")

    row = enabled.get(f"/api/chat/sessions/{chat['id']}").json()
    assert row["title"] == "add a note"
    assert row["usage"]["input_tokens"] == 100
    assert "turn" not in row["usage"]
    daily = enabled.get("/api/usage/daily", params={"days": 1, "source": "chat"}).json()
    assert [(r["model"], r["input_tokens"]) for r in daily["rows"]] == [("mock-worker", 100)]

    changes = enabled.get(f"/api/chat/sessions/{chat['id']}/changes").json()
    assert "mock-chat-1.md" in changes["uncommitted"]
    assert "+++ b/mock-chat-1.md" in changes["diff"] and "+add a note" in changes["diff"]
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

    async def record(self, text, images=None):
        sent.append(text)
        await original(self, text, images)

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


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="


def test_images_are_sent_and_kept_beside_the_transcript(enabled):
    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock", "permission": "yolo"}).json()
    image = {"data": PNG, "mime": "image/png"}
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "", "images": [image]})
        assert until(ws, "user")["images"] == ["1.png"]
        until(ws, "status", status="idle")
        ws.send_json({"type": "prompt", "text": "and this", "images": [image]})
        assert until(ws, "user")["images"] == ["2.png"]
        until(ws, "status", status="idle")
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")

    transcript = chat_service._transcript(chat["id"]).read_text()
    assert PNG not in transcript  # the transcript names the images rather than holding them
    assert "[1 image(s)]" in transcript  # the mock echoes what reached the driver
    assert enabled.get(f"/api/chat/sessions/{chat['id']}").json()["title"] == "Image"
    r = enabled.get(f"/api/chat/sessions/{chat['id']}/images/1.png")
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    for name in ("3.png", "events.jsonl", "..%2Fevents.jsonl", "1.svg"):
        assert enabled.get(f"/api/chat/sessions/{chat['id']}/images/{name}").status_code == 404


@pytest.mark.parametrize(
    ("images", "error"),
    [
        ([{"data": PNG, "mime": "image/svg+xml"}], "PNG, JPEG, GIF or WebP"),
        ([{"data": "not base64!", "mime": "image/png"}], "valid base64"),
        ([{"data": PNG, "mime": "image/png"}] * 5, "at most 4"),
    ],
)
def test_bad_images_are_refused(enabled, images, error):
    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock"}).json()
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "look", "images": images})
        assert error in until(ws, "error")["message"]
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    assert "look" not in chat_service._transcript(chat["id"]).read_text()


def test_images_are_refused_when_the_model_cannot_take_them(enabled, monkeypatch):
    monkeypatch.setattr(MockDriver, "images", False)
    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock"}).json()
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json(
            {"type": "prompt", "text": "look", "images": [{"data": PNG, "mime": "image/png"}]}
        )
        assert "doesn't take images" in until(ws, "error")["message"]
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


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


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@requires_git
def test_files_are_saved_where_the_agent_works_and_listed_for_it(enabled, project):
    sent = []
    original = MockDriver.prompt

    async def record(self, text, images=None):
        sent.append(text)
        await original(self, text, images)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MockDriver, "prompt", record)
        chat = start(enabled, project, permission="yolo")
        with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
            wait_ready(ws)
            files = [{"name": "../notes v2.md", "data": b64(b"# Notes\n")}]
            ws.send_json({"type": "prompt", "text": "read this", "files": files})
            assert until(ws, "user")["files"] == [".orchestrator/attachments/1-notes-v2.md"]
            until(ws, "status", status="idle")
            ws.send_json({"type": "prompt", "text": "", "files": files})
            assert until(ws, "user")["files"] == [".orchestrator/attachments/2-notes-v2.md"]
            until(ws, "status", status="idle")

    saved = Path(chat["cwd"]) / ".orchestrator" / "attachments" / "1-notes-v2.md"
    assert saved.read_bytes() == b"# Notes\n"
    assert sent[0] == (
        "read this\n\nAttached files, saved in your working directory:\n"
        "- .orchestrator/attachments/1-notes-v2.md"
    )
    assert sent[1].startswith("Attached files")
    # Typed text stays as typed, and the files never reach the branch.
    typed = [e["text"] for e in chat_service.history(chat["id"]) if e["type"] == "user"]
    assert typed == ["read this", ""]
    status = run_git(["status", "--porcelain", "--untracked-files=all"], Path(chat["cwd"]))
    assert ".orchestrator" not in status.stdout
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


def test_a_file_alone_names_the_session(enabled):
    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock", "permission": "yolo"}).json()
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        files = [{"name": "report.csv", "data": b64(b"a,b\n1,2\n")}]
        ws.send_json({"type": "prompt", "text": "", "files": files})
        until(ws, "status", status="idle")
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    assert enabled.get(f"/api/chat/sessions/{chat['id']}").json()["title"] == "1-report.csv"
    assert (Path(chat["cwd"]) / ".orchestrator/attachments/1-report.csv").is_file()


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ([{"name": "a.txt", "data": "not base64!"}], "valid base64"),
        ([{"name": "a.txt", "data": b64(b"x")}] * 6, "at most 5 files"),
        (["a.txt"], "missing its name or contents"),
    ],
)
def test_bad_files_are_refused(enabled, files, error):
    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock"}).json()
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "look", "files": files})
        assert error in until(ws, "error")["message"]
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    assert "look" not in chat_service._transcript(chat["id"]).read_text()


def test_attachment_limits_cover_images_and_files_together(monkeypatch):
    monkeypatch.setattr(chat_service, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(chat_service, "MAX_ATTACHMENTS_TOTAL", 75)
    with pytest.raises(chat_service.ChatError, match="under"):
        chat_service.parse_attachments([], [{"name": "big", "data": b64(b"x" * 11)}])
    image = {"data": PNG, "mime": "image/png"}  # 70 bytes
    with pytest.raises(chat_service.ChatError, match="must total"):
        chat_service.parse_attachments([image], [{"name": "f", "data": b64(b"x" * 10)}])
    images, files = chat_service.parse_attachments([image], [{"name": "f", "data": b64(b"x")}])
    assert len(images) == 1 and files[0].data == b"x"


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("../../etc/passwd", "passwd"),
        (".env", "env"),
        ("my report (final).pdf", "my-report-final-.pdf"),
        ("", "file"),
        ("a" * 120 + ".txt", "a" * 76 + ".txt"),  # 80 characters
    ],
)
def test_attached_file_names_are_made_safe(name, safe):
    assert chat_service._safe_name(name) == safe


def test_memory_suggestions_are_read_from_a_json_block_or_a_bare_array():
    parse = chat_service.parse_memory_suggestions
    reply = (
        "Sure.\n```json\n"
        '[{"category": "global", "scope": "preferences", "content": "Prefers uv."},'
        ' {"category": "nonsense", "scope": "x", "content": "dropped"}]\n```'
    )
    assert [s.content for s in parse(reply)] == ["Prefers uv."]
    bare = '[{"category": "role", "scope": "worker", "content": "Run ruff first."}]'
    assert parse(bare)[0].scope == "worker"
    assert parse("[]") == []
    assert parse("I couldn't think of anything.") is None
    many = json.dumps(
        [{"category": "global", "scope": "patterns", "content": f"{n}"} for n in range(9)]
    )
    assert len(parse(many)) == chat_service.MAX_MEMORY_SUGGESTIONS


def test_suggest_memories_asks_the_agent_and_stores_unapproved_entries(enabled, project):
    asked = []

    async def reply(self, text, images=None):
        asked.append(text)
        self.emit({"type": "status", "status": "busy"})
        answer = [
            {"category": "project", "scope": "gotchas", "content": "The API needs a token."},
            {"category": "global", "scope": "preferences", "content": "Keep replies short."},
        ]
        self.emit({"type": "text", "delta": f"```json\n{json.dumps(answer)}\n```"})
        self.emit({"type": "status", "status": "idle"})

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MockDriver, "prompt", reply)
        chat = start(enabled, project)
        with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
            wait_ready(ws)
            ws.send_json({"type": "suggest_memories"})
            assert until(ws, "user")["text"] == "Suggest memories from this session"
            assert "2 memory suggestions" in until(ws, "notice")["message"]
            # The same suggestions again are not duplicated.
            ws.send_json({"type": "suggest_memories"})
            assert until(ws, "notice")["message"] == "No new memory suggestions."
        enabled.post(f"/api/chat/sessions/{chat['id']}/close")

    assert '"project" (this session' in asked[0] and "JSON array" in asked[0]
    entries = enabled.get("/api/memory", params={"status": "pending"}).json()
    ours = [e for e in entries if e["source"] == f"chat:{chat['id']}"]
    assert {(e["category"], e["scope"]) for e in ours} == {
        ("project", "gotchas"),
        ("global", "preferences"),
    }
    assert all(not e["approved"] for e in ours)
    project_entry = next(e for e in ours if e["category"] == "project")
    assert project_entry["project_slug"] == project["slug"]


def test_suggest_memories_waits_for_the_turn_to_end(enabled):
    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock"}).json()
    with enabled.websocket_connect(f"/ws/chat/{chat['id']}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": "write something"})
        until(ws, "approval")  # the mock is mid-turn, waiting on an approval
        ws.send_json({"type": "suggest_memories"})
        assert "finish this turn" in until(ws, "error")["message"]
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")


def _turn(client, chat_id, text):
    with client.websocket_connect(f"/ws/chat/{chat_id}", headers=HOST) as ws:
        wait_ready(ws)
        ws.send_json({"type": "prompt", "text": text})
        approval = until(ws, "approval")
        ws.send_json({"type": "approval", "id": approval["id"], "choice": "allow"})
        until(ws, "status", status="idle")


def _continue(client, chat_id):
    r = client.post(f"/api/chat/sessions/{chat_id}/continue")
    assert r.status_code == 200, r.text
    with client.websocket_connect(f"/ws/chat/{chat_id}", headers=HOST) as ws:
        wait_ready(ws)
    return [e for e in client_history(chat_id) if e["type"] == "notice"][-1]["message"]


def client_history(chat_id):
    from orchestrator.services import chat_service

    return chat_service.history(chat_id)


def test_ended_session_continues_with_its_conversation(enabled):
    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock"}).json()
    folder = Path(chat["cwd"])
    _turn(enabled, chat["id"], "first note")
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    row = enabled.get(f"/api/chat/sessions/{chat['id']}").json()
    assert row["status"] == "closed" and row["harness_session"].startswith("mock-")

    # The harness restores its own conversation: the turn count carries on.
    assert "restored" in _continue(enabled, chat["id"])
    _turn(enabled, chat["id"], "second note")
    assert (folder / "mock-chat-2.md").read_text() == "second note\n"
    seqs = [e["seq"] for e in client_history(chat["id"])]
    assert seqs == sorted(set(seqs))  # appended, never renumbered

    # When it can't, the next message carries the transcript instead.
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    for saved in (folder.parent / "harness").iterdir():
        saved.unlink()
    assert "couldn't restore" in _continue(enabled, chat["id"])
    _turn(enabled, chat["id"], "third note")
    sent = (folder / "mock-chat-1.md").read_text()  # a fresh harness numbers from 1 again
    assert "<transcript>" in sent and "User: first note" in sent
    assert sent.rstrip().endswith("third note")
    assert enabled.post(f"/api/chat/sessions/{chat['id']}/continue").status_code == 400  # live


def test_session_without_a_saved_conversation_replays_it(enabled):
    from orchestrator.core.db import session_scope
    from orchestrator.core.models import ChatSession

    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock"}).json()
    _turn(enabled, chat["id"], "first note")
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    with session_scope() as s:  # as for sessions from before Continue existed
        row = s.get(ChatSession, chat["id"])
        row.harness_session = None
        s.add(row)
        s.commit()
    assert "can't restore" in _continue(enabled, chat["id"])


def test_failed_and_standalone_sessions_cannot_continue(enabled):
    from orchestrator.core.db import session_scope
    from orchestrator.core.models import ChatSession

    chat = enabled.post("/api/chat/sessions", json={"adapter": "mock"}).json()
    enabled.post(f"/api/chat/sessions/{chat['id']}/close")
    for field, value in (("status", "failed"), ("standalone", True)):
        with session_scope() as s:
            row = s.get(ChatSession, chat["id"])
            row.status = "closed"
            setattr(row, field, value)
            s.add(row)
            s.commit()
        r = enabled.post(f"/api/chat/sessions/{chat['id']}/continue")
        assert r.status_code == 400, r.text
