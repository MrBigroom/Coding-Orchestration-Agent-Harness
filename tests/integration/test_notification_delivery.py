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


def _create_app():
    from orchestrator.api.main import create_app

    return create_app()


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
    from orchestrator.core.db import session_scope

    with TestClient(_create_app(), base_url="http://127.0.0.1:8321") as client:
        with session_scope() as s:
            s.add(_pending("read:1", title="read path pending"))
            s.commit()
        assert sent == []  # inserted after startup, so only the read path can deliver it
        rows = client.get("/api/notifications").json()
        assert [(r["title"], r["status"]) for r in rows] == [("read path pending", "delivered")]
    assert sent == ["read path pending"]


def test_cli_exit_delivers_and_terminates_only_its_own_rows(initialized, sent, monkeypatch):
    from orchestrator import cli
    from orchestrator.core.db import session_scope

    with session_scope() as s:
        s.add(_pending("cli:1", title="cli exit pending"))
        s.commit()
    monkeypatch.setattr(cli, "app", lambda: None)
    cli.main()
    with session_scope() as s:
        row = s.exec(select(Notification).where(Notification.dedupe_key == "cli:1")).one()
        assert (row.status, row.reason) == ("delivered", "")

    monkeypatch.setattr(notification_service, "flush_pending", lambda session=None: 0)
    with session_scope() as s:
        s.add(_pending("cli:2", title="cannot deliver"))
        s.add(_pending("server:2", owner="server:pid", title="live server row"))
        s.commit()
    cli.main()
    with session_scope() as s:
        own = s.exec(select(Notification).where(Notification.dedupe_key == "cli:2")).one()
        other = s.exec(select(Notification).where(Notification.dedupe_key == "server:2")).one()
        assert (own.status, own.reason) == ("failed", "process_exit_undelivered")
        assert (other.status, other.reason) == ("pending", "")


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
