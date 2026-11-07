"""T-9 end-to-end: the mock harness produces exactly the expected notification records."""

from __future__ import annotations

import asyncio
import html
import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import col, select

from orchestrator.core.models import Notification, Run
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
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


def _create_app():
    """Build the app on call, never at import time.

    A module-level `from orchestrator.api.main import create_app` runs during collection, while the
    working directory is still the repo, so core.config.get_settings() -> load_env_file() reads a
    developer `.env` into os.environ — and that survives conftest's chdir. That is how the T-9 merge
    turned tests/integration/test_sync_api.py red in a checkout with a `.env` while every gated run
    (a worktree, which never receives the gitignored `.env`) stayed green. tests/conftest.py now
    clears the leaked keys; importing here keeps this file off that list entirely.
    """
    from orchestrator.api.main import create_app

    return create_app()


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(tmp_path / "proj", {"run_tests.py": RUN_TESTS, "README.md": "x\n"})
    return project_service.create(
        session, ProjectCreate(name="proj", path=str(repo), test_command="python3 run_tests.py")
    )


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict[str, str]] = []

    def fake_deliver(title: str, body: str, link: str = "") -> notification_service.TransportResult:
        calls.append({"title": title, "body": body, "link": link})
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel", None, "plain")

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
    session.expire_all()
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
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    task = _task(session, project, ["mock"], 1, "notify")
    outcome = asyncio.run(run_service.run_task_now(session, task))
    assert outcome["decision"] == "accepted"
    notification_service.flush_pending(session)

    assert len(sent) == 1
    for trigger in ("task_review", "merge_candidate", "run_completed"):
        assert f"[{trigger}]" in sent[0]["body"]
    # DoD 9 end-to-end: the live terminal row carries both windows, and the compute figure is
    # the archived one the deadline actually used (run.json is written BEFORE the terminal event,
    # so it is available at fan-out time).
    compute_ms = artifact_service.read_json(outcome["run_id"], "run.json")["process_ms"]
    assert (
        f"Duration: {notification_service._human(compute_ms)} compute / " in sent[0]["body"]
    )
    assert sent[0]["link"].endswith(f"/tasks/{task.id}")
    active = [r for r in _rows(session, task) if r.status != "skipped"]
    assert [r.trigger for r in active] == ["task_review", "merge_candidate", "run_completed"]
    assert [r.status for r in active] == ["delivered", "coalesced", "coalesced"]
    assert active[0].run_id == outcome["run_id"]
    assert all(
        r.reason == "trigger_disabled" for r in _rows(session, task) if r.status == "skipped"
    )


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


def test_startup_recovery_delivers_rows_a_dead_cli_process_left_pending(initialized, sent):
    from orchestrator.core.db import session_scope

    with session_scope() as s:
        s.add(
            Notification(
                trigger="run_completed",
                event="run_completed",
                status="pending",
                title="left pending",
                body="from a dead CLI timer",
                dedupe_key="recovery:1",
                details_json={},
            )
        )
        s.commit()
    with TestClient(_create_app(), base_url="http://127.0.0.1:8321") as client:
        assert client.get("/api/health").status_code == 200
    assert sent and sent[0]["title"] == "left pending"
    with session_scope() as s:
        assert (
            s.exec(select(Notification).where(Notification.dedupe_key == "recovery:1")).one().status
            == "delivered"
        )


def test_notifications_endpoint_serves_the_persistent_list(initialized):
    from orchestrator.core.db import session_scope

    with TestClient(_create_app(), base_url="http://127.0.0.1:8321") as client:
        with session_scope() as s:
            s.add(
                Notification(
                    trigger="run_completed",
                    event="run_completed",
                    status="delivered",
                    title="T-1 — Run completed",
                    body="Outcome: done",
                    dedupe_key="api:1",
                    details_json={},
                )
            )
            s.commit()
        rows = client.get("/api/notifications").json()
        assert [(r["trigger"], r["status"]) for r in rows] == [("run_completed", "delivered")]
        assert client.get("/api/notifications?status=bogus").status_code == 422


def test_the_task_link_is_a_real_target_when_web_dist_is_absent(initialized, monkeypatch):
    """DoD 5: call the deep link instead of grepping the page for a source string.

    With no web/dist, `/tasks/7` is served by the fallback UI. The API resolves the path into the
    page's `data-route`, so asking for the URL asserts the view the page will render. The React
    side is matched against the route table `web/src/App.tsx` declares (there is no JS runner).
    """
    from orchestrator.api import main as api_main

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(api_main, "frontend_dist_dir", lambda: None)
    with TestClient(api_main.create_app(), base_url="http://127.0.0.1:8321") as client:
        res = client.get("/tasks/7")
    assert res.status_code == 200 and "React build not found" in res.text  # the fallback UI
    route = json.loads(html.unescape(re.search(r'data-route="([^"]+)"', res.text).group(1)))
    assert route == {"view": "task", "arg": "7"}
    assert "async task(" in res.text  # the page can render the resolved view
    routes = re.findall(
        r'<Route\s+path="([^"]+)"', (root / "web" / "src" / "App.tsx").read_text()
    )
    assert [p for p in routes if p.startswith("/tasks/")] == ["/tasks/:id"]
    # No JS test runner exists, so the React side is matched from its declared route table.
    web_pkg = json.loads((root / "web" / "package.json").read_text(encoding="utf-8"))
    assert "test" not in web_pkg["scripts"]
