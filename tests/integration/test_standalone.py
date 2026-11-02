from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from orchestrator.adapters.chat.mock import OPTIONS, MockDriver
from orchestrator.core.db import session_scope
from orchestrator.core.models import ChatSession, Schedule, StandaloneRun, Task
from orchestrator.services import git_service, standalone_service, sync_service
from orchestrator.services.standalone_service import decide
from orchestrator.utils.git import run_git
from tests.conftest import make_repo, requires_git


@pytest.fixture
def client(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as c:
        c.patch("/api/settings", json={"values": {"allow_unattended_sessions": True}})
        yield c


@pytest.fixture
def project(client, tmp_path):
    repo = make_repo(tmp_path / "repo")
    return client.post("/api/projects", json={"name": "demo", "path": str(repo)}).json()


@pytest.fixture
def hanging(monkeypatch):
    async def forever(self, text, images=None):
        await asyncio.sleep(3600)

    monkeypatch.setattr(MockDriver, "_run", forever)


def make_agent(client, project=None):
    body = {
        "name": "reporter",
        "instructions": "Report plainly.",
        "harness_adapter": "mock",
        "project_id": project["id"] if project else None,
    }
    r = client.post("/api/agents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def make_schedule(client, agent, **fields):
    body = {
        "name": "report",
        "cron_expression": "0 2 * * *",
        "agent_id": agent["id"],
        "run_mode": "standalone",
        "task_template_json": {"description": "Summarise the repo"},
        **fields,
    }
    r = client.post("/api/schedules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def run_now(client, schedule):
    r = client.post(f"/api/schedules/{schedule['id']}/run")
    assert r.status_code == 200, r.text
    return r.json()


def finished(client, agent):
    for _ in range(300):
        runs = client.get(f"/api/agents/{agent['id']}/outputs").json()["runs"]
        if runs and runs[0]["status"] != "running":
            return runs[0]
        time.sleep(0.02)
    raise AssertionError("the run never finished")


def chat_of(run) -> ChatSession:
    with session_scope() as s:
        return s.get(ChatSession, run["chat_id"])


@pytest.mark.parametrize(
    ("access", "action", "allowed"),
    [
        ("read", {"kind": "read", "paths": []}, True),
        ("read", {"kind": "other", "paths": []}, False),
        ("read", None, False),
        ("read", {"kind": "write", "paths": []}, False),
        ("read", {"kind": "write", "paths": ["notes.md"]}, False),
        ("read", {"kind": "write", "paths": ["/out/notes.md"]}, True),
        ("read", {"kind": "write", "paths": ["/out/../secret"]}, False),
        ("read", {"kind": "write", "paths": ["/out/a", "/etc/passwd"]}, False),
        ("write", {"kind": "write", "paths": ["notes.md"]}, True),
        ("write", {"kind": "write", "paths": ["/out/notes.md"]}, True),
        ("write", {"kind": "write", "paths": ["/etc/passwd"]}, False),
        ("write", {"kind": "other", "paths": []}, True),
        ("write", None, True),
    ],
)
def test_decide(access, action, allowed):
    assert decide(access, action, Path("/work"), Path("/out")) is allowed


def test_decide_follows_symlinks_out_of_the_folder(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "escape").symlink_to(tmp_path)
    action = {"kind": "write", "paths": [str(output / "escape" / "file")]}
    assert decide("read", action, tmp_path / "work", output) is False


def test_permission_settings_stay_on_each_device():
    assert {"allow_interactive_sessions", "allow_unattended_sessions"} <= (
        sync_service.LOCAL_ONLY_SETTINGS
    )


def test_off_by_default_and_recorded_as_skipped(client, project):
    client.patch("/api/settings", json={"values": {"allow_unattended_sessions": False}})
    agent = make_agent(client, project)
    run = run_now(client, make_schedule(client, agent))
    assert run["status"] == "skipped" and "allow_unattended_sessions" in run["error"]
    assert run["chat_id"] is None


def test_standalone_needs_an_agent(client):
    r = client.post(
        "/api/schedules",
        json={"name": "x", "cron_expression": "0 2 * * *", "run_mode": "standalone"},
    )
    assert r.status_code == 400


@requires_git
def test_read_access_denies_changes_and_saves_a_report(client, project):
    agent = make_agent(client, project)
    run_now(client, make_schedule(client, agent))
    run = finished(client, agent)

    assert run["status"] == "completed" and run["adapter"] == "mock"
    assert "Allow write" in run["blocked"][0]
    assert "Report plainly." in run["summary"]  # the mock echoes its prompt
    assert run["usage"]["input_tokens"] == 100
    report = client.get(f"/api/standalone-runs/{run['id']}/report").json()["text"]
    assert "Status: completed" in report and "## Blocked" in report
    assert Path(run["report_path"]).is_file()

    chat = chat_of(run)
    assert chat.standalone and chat.status == "closed" and chat.workspace == "snapshot"
    assert not Path(chat.cwd).exists()  # the snapshot is gone
    branches = run_git(["branch", "--list", "chat/*"], Path(project["path"])).stdout
    assert branches.strip() == ""
    with session_scope() as s:
        assert not list(s.exec(select(Task)))


@requires_git
def test_write_access_keeps_changes_on_a_branch(client, project):
    agent = make_agent(client, project)
    run_now(client, make_schedule(client, agent, access="write"))
    run = finished(client, agent)

    assert run["status"] == "completed" and run["blocked"] == []
    chat = chat_of(run)
    assert chat.workspace == "worktree" and chat.branch_name.startswith("chat/")
    assert (Path(chat.cwd) / "mock-chat-1.md").is_file()
    assert not (Path(project["path"]) / "mock-chat-1.md").exists()


def test_a_turn_that_ends_on_a_denial_is_told_to_continue(client, project, monkeypatch):
    prompts = []

    async def stops_when_denied(self, text, images=None):
        prompts.append(text)
        try:
            if len(prompts) == 1:
                self._answer = asyncio.get_running_loop().create_future()
                self.ask("write-1", "Allow write?", "x", OPTIONS, {"kind": "other", "paths": []})
                await self._answer
            else:
                self.emit({"type": "text", "delta": "The final report."})
        finally:
            self.emit({"type": "status", "status": "idle"})

    monkeypatch.setattr(MockDriver, "_run", stops_when_denied)
    agent = make_agent(client, project)
    run_now(client, make_schedule(client, agent))
    run = finished(client, agent)
    assert len(prompts) == 2 and "denied" in prompts[1]
    assert run["status"] == "completed" and run["summary"] == "The final report."


def test_approvals_are_answered_while_a_prompt_is_in_progress(client, project, monkeypatch):
    """ACP's prompt call only returns when the turn ends, so it can't hold up the watcher."""
    turns = []

    async def blocking_prompt(self, text, images=None):
        self.emit({"type": "status", "status": "busy"})
        turns.append(text)
        self._answer = asyncio.get_running_loop().create_future()
        self.ask("write-1", "Allow write?", "x", OPTIONS, {"kind": "other", "paths": []})
        await self._answer
        if len(turns) > 1:
            self.emit({"type": "text", "delta": "Done."})
        self.emit({"type": "status", "status": "idle"})

    monkeypatch.setattr(MockDriver, "prompt", blocking_prompt)
    agent = make_agent(client, project)
    run_now(client, make_schedule(client, agent))
    run = finished(client, agent)
    assert len(turns) == 2 and run["status"] == "completed" and run["summary"] == "Done."


def test_nudges_are_limited(client, project, monkeypatch):
    prompts = []

    async def always_denied(self, text, images=None):
        prompts.append(text)
        try:
            self._answer = asyncio.get_running_loop().create_future()
            self.ask("write-1", "Allow write?", "x", OPTIONS, {"kind": "other", "paths": []})
            await self._answer
        finally:
            self.emit({"type": "status", "status": "idle"})

    monkeypatch.setattr(MockDriver, "_run", always_denied)
    agent = make_agent(client, project)
    run_now(client, make_schedule(client, agent))
    finished(client, agent)
    assert len(prompts) == 1 + standalone_service.MAX_NUDGES


def test_no_project_works_in_the_output_folder(client):
    agent = make_agent(client)
    run_now(client, make_schedule(client, agent))
    run = finished(client, agent)

    folder = client.get(f"/api/agents/{agent['id']}/outputs").json()["folder"]
    assert run["status"] == "completed" and run["blocked"] == []
    assert run["files"] == ["mock-chat-1.md"] and (Path(folder) / "mock-chat-1.md").is_file()
    chat = chat_of(run)
    assert chat.project_id is None
    assert client.get(f"/api/chat/sessions/{chat.id}/changes").status_code == 200
    assert client.delete(f"/api/chat/sessions/{chat.id}").status_code == 204


def test_a_failed_start_is_recorded(client, monkeypatch):
    agent = make_agent(client)

    async def broken(self):
        raise RuntimeError("no harness here")

    monkeypatch.setattr(MockDriver, "start", broken)
    run_now(client, make_schedule(client, agent))
    run = finished(client, agent)
    assert run["status"] == "failed" and "no harness here" in run["error"]


def test_no_supported_harness_is_recorded(client):
    agent = make_agent(client)
    client.patch(f"/api/agents/{agent['id']}", json={"harness_adapter": "aider"})
    run = run_now(client, make_schedule(client, agent))
    assert run["status"] == "failed" and "no harness can run" in run["error"]


def test_overlapping_runs_are_skipped(client, hanging):
    agent = make_agent(client)
    schedule = make_schedule(client, agent)
    first = run_now(client, schedule)
    second = run_now(client, schedule)
    assert first["status"] == "running"
    assert second["status"] == "skipped" and "still going" in second["error"]
    client.post(f"/api/standalone-runs/{first['id']}/stop")


def test_stop_ends_a_run(client, hanging):
    agent = make_agent(client)
    run = run_now(client, make_schedule(client, agent))
    stopped = client.post(f"/api/standalone-runs/{run['id']}/stop").json()
    assert stopped["status"] == "stopped" and stopped["report_path"]
    assert client.post(f"/api/standalone-runs/{run['id']}/stop").status_code == 400


def test_the_time_limit_stops_a_run(client, hanging):
    agent = make_agent(client)
    schedule = make_schedule(client, agent)
    with session_scope() as s:
        row = s.get(Schedule, schedule["id"])
        row.time_limit_minutes = 0.001
        s.add(row)
        s.commit()
    run_now(client, schedule)
    run = finished(client, agent)
    assert run["status"] == "timeout" and "time limit" in run["error"]


def test_a_standalone_run_takes_no_instructions(client, hanging):
    agent = make_agent(client)
    run = run_now(client, make_schedule(client, agent))
    with client.websocket_connect(
        f"/ws/chat/{run['chat_id']}", headers={"host": "127.0.0.1:8321"}
    ) as ws:
        assert ws.receive_json()["type"] == "history"
        ws.send_json({"type": "prompt", "text": "do something else"})
        for _ in range(20):
            event = ws.receive_json()
            if event["type"] == "error":
                break
        assert "can only be stopped" in event["message"]
        ws.send_json({"type": "interrupt"})
        for _ in range(20):
            if ws.receive_json()["type"] == "end":
                break
    assert finished(client, agent)["status"] == "stopped"


def test_one_time_standalone_schedule_turns_off(client):
    agent = make_agent(client)
    schedule = make_schedule(client, agent, cron_expression="", run_at="2099-01-01T00:00")
    with session_scope() as s:
        row = s.get(Schedule, schedule["id"])
        row.run_at = row.run_at.replace(year=2020)
        s.add(row)
        s.commit()
    run_now(client, schedule)
    finished(client, agent)
    assert client.get("/api/schedules").json()[0]["enabled"] is False


def test_recover_marks_cut_off_runs(initialized):
    with session_scope() as s:
        s.add(StandaloneRun(agent_id=1, title="t"))
        s.commit()
        standalone_service.recover(s)
        run = s.exec(select(StandaloneRun)).one()
        assert run.status == "interrupted" and run.finished_at is not None


@requires_git
def test_snapshot_is_detached_and_leaves_no_branch(tmp_path, initialized):
    repo = make_repo(tmp_path / "r")
    info = git_service.ensure_snapshot(repo, "demo", "chat-1", "main")
    assert run_git(["rev-parse", "--abbrev-ref", "HEAD"], info.path).stdout.strip() == "HEAD"
