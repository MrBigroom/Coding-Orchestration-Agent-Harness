"""T-18: truthful delivery outcomes, the capability probe and the pending-row flush points.

Kept compact on purpose: the review diff has a fixed token budget, so the DoD-critical
assertions live here instead of being spread over the older T-9 file.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from sqlmodel import col, select

from orchestrator.core.models import Agent, Notification, Run, StandaloneRun, StandaloneStatus
from orchestrator.core.notification_vocab import MASTER_SETTING
from orchestrator.core.schemas import TaskCreate
from orchestrator.services import log_service, notification_service, settings_service, task_service

LINK = "http://127.0.0.1:8321/tasks/7"
# Captured before the autouse conftest fixture replaces it with an offline stub.
REAL_PROBE = notification_service.probe_capabilities


@pytest.fixture(autouse=True)
def _no_timer(monkeypatch):
    monkeypatch.setattr(notification_service, "_timer", None)
    monkeypatch.setattr(notification_service, "_batch_started", None)


def _caps(*names):
    return notification_service.DaemonCapabilities(True, frozenset(names), "GetCapabilities")


def _clients(monkeypatch, *, present=("notify-send", "gdbus"), caps=None, replies=()):
    seen: list[list[str]] = []
    queued = list(replies)

    def run(argv):
        seen.append(argv)
        return queued.pop(0) if queued else (argv[0], None, "")

    monkeypatch.setattr(notification_service, "_run_client", run)
    monkeypatch.setattr(
        notification_service.shutil, "which", lambda n: n if n in present else None
    )
    monkeypatch.setattr(
        notification_service,
        "probe_capabilities",
        lambda: caps or notification_service.UNPROBED,
    )
    return seen


def test_probe_parses_capabilities_and_fails_closed(monkeypatch):
    real = REAL_PROBE
    monkeypatch.setattr(notification_service, "probe_capabilities", real)
    monkeypatch.setattr(notification_service.shutil, "which", lambda n: n)

    class Reply:
        def __init__(self, rc, out, err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr(
        notification_service.subprocess,
        "run",
        lambda *a, **k: Reply(0, "(['body', 'body-markup', 'body-hyperlinks'],)\n"),
    )
    caps = real()
    assert caps.probed and caps.supports("body-markup") and caps.supports("body-hyperlinks")
    monkeypatch.setattr(
        notification_service.subprocess, "run", lambda *a, **k: Reply(1, "", "no")
    )
    assert real().probed is False

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("gdbus", 2)

    monkeypatch.setattr(notification_service.subprocess, "run", boom)
    assert real().probed is False
    monkeypatch.setattr(notification_service.shutil, "which", lambda n: None)
    assert real().source == "gdbus not on PATH"


def test_markup_daemon_gets_escaped_text_on_every_transport_and_no_hint(monkeypatch):
    body = "<b>x</b> & \"q\"\nOpen: " + LINK
    seen = _clients(monkeypatch, present={"notify-send"}, caps=_caps("body-markup", "body-hyperlinks"))
    result = notification_service.deliver("t", body, LINK)
    assert (result.shape, result.channel) == ("markup-escaped", "notify-send")
    assert seen[0][-1] == "&lt;b&gt;x&lt;/b&gt; &amp; \"q\"\nOpen: " + LINK

    seen = _clients(
        monkeypatch,
        present={"gdbus"},
        caps=_caps("body-markup"),
        replies=[("gdbus", None, "(uint32 7,)\n")],
    )
    result = notification_service.deliver("t", body, LINK)
    assert (result.shape, result.status) == ("markup-escaped", "delivered")
    assert seen[0][-2] == "{}"  # body-markup is a capability, never a hint

    seen = _clients(
        monkeypatch,
        present={"gdbus"},
        caps=_caps("body-markup", "body-hyperlinks"),
        replies=[("gdbus", None, "(uint32 8,)\n")],
    )
    assert notification_service.deliver("t", body, LINK).shape == "markup+hyperlink"
    assert seen[0][-2] == "{}"
    assert '<a href="' + LINK + '">Open task</a>' in json.loads(seen[0][-4])

    seen = _clients(monkeypatch, present={"notify-send"}, caps=notification_service.UNPROBED)
    notification_service.deliver("t", "raw <b>")
    assert seen[0][-1] == "raw <b>"  # an unprobed daemon is not assumed to parse markup


def test_every_transport_result_maps_to_one_frozen_status(monkeypatch):
    assert set(notification_service.STATUS_FOR_OUTCOME) == set(notification_service.OUTCOME_CLASSES)
    assert set(notification_service.STATUS_FOR_OUTCOME.values()) <= {
        "pending",
        "delivered",
        "failed",
        "skipped",
        "coalesced",
    }
    cases = {}
    _clients(monkeypatch, present={"notify-send"})
    cases["shown"] = notification_service.deliver("t", "b", LINK)
    _clients(monkeypatch, present={"gdbus"}, replies=[("gdbus", None, "(uint32 42,)\n")])
    cases["id"] = notification_service.deliver("t", "b", LINK)
    _clients(monkeypatch, present={"notify-send"}, replies=[("notify-send", "exit 1", "")])
    cases["exit"] = notification_service.deliver("t", "b", LINK)
    _clients(monkeypatch, present={"notify-send"}, replies=[("notify-send", "timed out", "")])
    cases["timeout"] = notification_service.deliver("t", "b", LINK)
    _clients(monkeypatch, present=())
    cases["missing"] = notification_service.deliver("t", "b", LINK)
    assert {name: (r.status, r.reason) for name, r in cases.items()} == {
        "shown": ("delivered", "shown_unacknowledged"),
        "id": ("delivered", ""),
        "exit": ("failed", "delivery_failed"),
        "timeout": ("failed", "delivery_failed"),
        "missing": ("failed", "delivery_failed"),
    }
    assert cases["id"].detail == "daemon accepted notification id 42"


def _task_run(session, run_id="R-1"):
    task = task_service.create(
        session, TaskCreate(title="t", phase_number=1, phase_slug="notify")
    )
    run = Run(short_id=run_id, task_id=task.id, status="completed", exit_code=0)
    session.add(run)
    session.commit()
    session.refresh(task)
    session.refresh(run)
    return task, run


def _rows(session):
    session.expire_all()
    return list(session.exec(select(Notification).order_by(col(Notification.id))))


def test_a_displayed_message_is_recorded_delivered_not_failed(session, monkeypatch):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    _clients(monkeypatch, present={"notify-send"})
    log_service.log_event(
        session, "run_timeout", "timed out", role="worker", task_id=task.id, run_id=run.id
    )
    row = _rows(session)[0]
    assert (row.status, row.reason, row.error) == ("delivered", "shown_unacknowledged", None)


def test_the_standalone_path_reaches_the_same_outcome_classes(session, monkeypatch, tmp_path):
    agent = Agent(name="reporter", instructions="")
    session.add(agent)
    session.commit()
    session.refresh(agent)
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")
    run = StandaloneRun(
        agent_id=agent.id or 0,
        title="summarise",
        status=StandaloneStatus.completed,
        report_path=str(report),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    _clients(monkeypatch, present={"gdbus"})
    notification_service.notify_event(
        event="standalone_run", details=f"{run.title}: completed", standalone_run_id=run.id
    )
    row = _rows(session)[0]
    assert (row.status, row.reason, row.link.endswith("/agents")) == (
        "delivered",
        "shown_unacknowledged",
        True,
    )


def _pending(session, key, owner=None, title="pending row"):
    row = Notification(
        trigger="run_completed",
        event="run_completed",
        status="pending",
        title=title,
        body="from a dead CLI timer",
        dedupe_key=key,
        details_json={notification_service.OWNER_KEY: owner or notification_service.PROCESS_TOKEN},
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_exit_flush_delivers_own_rows_and_terminates_only_what_it_owns(session, monkeypatch):
    mine = _pending(session, "exit:1")
    other = _pending(session, "server:1", owner="server:pid")
    monkeypatch.setattr(notification_service, "flush_pending", lambda session=None: 0)
    assert notification_service.flush_pending_at_exit(session) == 1
    assert (mine.status, mine.reason) == ("failed", "process_exit_undelivered")
    assert (other.status, other.reason) == ("pending", "")  # a live server keeps its row


def test_exit_flush_delivers_a_pending_row(session, monkeypatch):
    _pending(session, "exit:2")
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    _clients(monkeypatch, present={"notify-send"})
    assert notification_service.flush_pending_at_exit(session) == 1
    assert _rows(session)[0].status == "delivered"


def test_exit_flush_is_inert_before_the_database_exists(home):
    assert notification_service.flush_pending_at_exit() == 0
    assert not (home / "orchestrator.db").exists()


def test_parallel_finishes_across_runs_are_one_message(session, monkeypatch):
    calls = []

    def fake_deliver(title, body, link=""):
        calls.append(title)
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel"
        )

    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    settings_service.update(session, {MASTER_SETTING: True})
    runs = [_task_run(session, f"R-{i}")[1] for i in range(5)]
    for run in runs:
        notification_service.notify_event(event="run_completed", run_id=run.id)
    notification_service.flush_pending(session)
    assert len(calls) == 1 and "5 updates" in calls[0]
    rows = _rows(session)
    assert [r.status for r in rows] == ["delivered"] + ["coalesced"] * 4
    assert len({r.dedupe_key for r in rows}) == 5  # one row per run per trigger
