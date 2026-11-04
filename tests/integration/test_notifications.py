"""T-9 end-to-end: the mock harness produces exactly the expected notification records."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlmodel import col, select

from orchestrator.core.models import Notification, Run
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import (
    log_service,
    notification_service,
    project_service,
    run_service,
    settings_service,
    task_service,
)
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

RUN_TESTS = "import sys; print('2 passed, 0 failed'); sys.exit(0)\n"


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(tmp_path / "proj", {"run_tests.py": RUN_TESTS, "README.md": "x\n"})
    return project_service.create(
        session, ProjectCreate(name="proj", path=str(repo), test_command="python3 run_tests.py")
    )


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict[str, str]] = []

    def fake_deliver(title: str, body: str, link: str = "") -> tuple[str, None]:
        calls.append({"title": title, "body": body, "link": link})
        return "test-channel", None

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    return calls


def _task(session, project, tags, phase, slug):
    return task_service.create(
        session,
        TaskCreate(
            title=f"notification phase {phase}",
            project_id=project.id,
            tags=tags,
            phase_number=phase,
            phase_slug=slug,
            definition_of_done=["MOCK_CHANGE.md updated"],
        ),
    )


def _rows(session, task) -> list[Notification]:
    return list(
        session.exec(
            select(Notification)
            .where(Notification.task_id == task.id)
            .order_by(col(Notification.id))
        )
    )


def test_completed_mock_run_yields_one_coalesced_notification(session, project, sent, monkeypatch):
    settings_service.update(session, {"notifications_enabled": True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda key: None)
    task = _task(session, project, ["mock"], 1, "notify")
    outcome = asyncio.run(run_service.run_task_now(session, task))
    assert outcome["decision"] == "accepted"
    notification_service.flush_pending(session)

    assert len(sent) == 1
    for trigger in ("task_review", "merge_candidate", "run_completed"):
        assert f"[{trigger}]" in sent[0]["body"]
    active = [r for r in _rows(session, task) if r.status != "skipped"]
    assert [r.trigger for r in active] == ["task_review", "merge_candidate", "run_completed"]
    assert [r.status for r in active] == ["delivered", "coalesced", "coalesced"]
    assert active[0].run_id == outcome["run_id"]
    assert all(r.reason == "trigger_disabled" for r in _rows(session, task) if r.status == "skipped")


def test_cancelled_run_emits_run_cancelled_once_and_fans_out(session, project, sent, monkeypatch):
    settings_service.update(session, {"notifications_enabled": True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    task = _task(session, project, ["mock-timeout"], 9, "cancel-notify")

    def cancel_soon():
        time.sleep(0.6)
        from orchestrator.core.db import session_scope

        with session_scope() as s:
            run_service.cancel(s, task_service.get(s, task.id))

    threading.Thread(target=cancel_soon).start()
    outcome = asyncio.run(run_service.run_task_now(session, task))
    assert outcome["status"] == "cancelled"
    cancelled = [
        e
        for e in log_service.recent(session, limit=400, task_id=task.id)
        if e.event == "run_cancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0].run_id == outcome["run_id"]
    assert f"R-{outcome['run_id']}" in cancelled[0].details
    assert session.get(Run, outcome["run_id"]).status == "cancelled"
    active = [r for r in _rows(session, task) if r.status != "skipped"]
    assert [r.trigger for r in active] == ["run_cancelled"]
    assert active[0].status == "delivered"
    assert "cancelled" in sent[0]["body"] and "Next:" in sent[0]["body"]
