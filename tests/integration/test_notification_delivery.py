"""T-18 end-to-end: the pending-row flush points and the CLI entry points."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from orchestrator.core.models import Notification
from orchestrator.services import notification_service
from tests.conftest import requires_git

pytestmark = requires_git


@pytest.fixture
def sent(monkeypatch):
    calls: list[str] = []

    def fake_deliver(title, body, link=""):
        calls.append(title)
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel"
        )

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    return calls


def _pending(key, owner=None, title="left pending"):
    return Notification(
        trigger="run_completed",
        event="run_completed",
        status="pending",
        title=title,
        body="from a dead CLI timer",
        dedupe_key=key,
        details_json={notification_service.OWNER_KEY: owner or notification_service.PROCESS_TOKEN},
    )


def test_reading_the_list_flushes_rows_the_server_left_pending(initialized, sent):
    from orchestrator.api.main import create_app
    from orchestrator.core.db import session_scope

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as client:
        with session_scope() as s:
            s.add(_pending("read:1", title="read path pending"))
            s.commit()
        assert sent == []  # inserted after startup, so only the read path can deliver it
        rows = client.get("/api/notifications").json()
        assert [(r["title"], r["status"]) for r in rows] == [("read path pending", "delivered")]
    assert sent == ["read path pending"]


def test_cli_exit_delivers_a_row_the_production_path_created(initialized, sent, monkeypatch):
    """Finding 2: the row comes from `notify_event`, so the owner stamp and the exit

    flush are exercised together. If production stops stamping the owner the flush finds
    nothing and this test fails, instead of passing on a hand-stamped fixture.
    """
    from orchestrator import cli
    from orchestrator.core.db import session_scope
    from orchestrator.core.notification_vocab import MASTER_SETTING
    from orchestrator.services import settings_service

    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    with session_scope() as s:
        settings_service.update(s, {MASTER_SETTING: True})
    row = notification_service.notify_event(event="run_completed", details="finished")
    assert row is not None and row.id is not None
    with session_scope() as s:
        pending = s.exec(select(Notification)).one()
        assert (pending.status, pending.reason) == ("pending", "")
        # The real fan-out stamped this process's owner token; nothing is hand-set here.
        assert pending.details_json[notification_service.OWNER_KEY] == (
            notification_service.PROCESS_TOKEN)
        title = pending.title

    monkeypatch.setattr(cli, "app", lambda: None)
    cli.main()

    with session_scope() as s:
        delivered = s.exec(select(Notification)).one()
        assert (delivered.status, delivered.reason) == ("delivered", "")
    assert sent == [title]


def test_python_dash_m_and_the_console_script_route_through_cli_main(initialized):
    root = Path(__file__).resolve().parents[2]
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared["project"]["scripts"]["orchestrator"] == "orchestrator.cli:main"
    code = (
        "import runpy; import orchestrator.cli as c; "
        "c.main = lambda: print('CLI_MAIN_CALLED'); "
        "runpy.run_module('orchestrator', run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        env={**os.environ, "ORCHESTRATOR_HOME": os.environ["ORCHESTRATOR_HOME"]},
    )
    assert result.returncode == 0, result.stderr
    assert "CLI_MAIN_CALLED" in result.stdout
