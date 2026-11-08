"""T-9: triggers, payload/redaction, coalescing, channel choice, failure isolation, duration."""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from sqlmodel import col, select

from orchestrator.core.models import (
    Agent,
    BuildLogEntry,
    Notification,
    Run,
    StandaloneRun,
    StandaloneStatus,
    TaskStatus,
)
from orchestrator.core.notification_vocab import (
    CORE_TRIGGERS,
    DIRECT_EVENTS,
    MASTER_SETTING,
    NOISY_TRIGGERS,
    TRIGGERS,
    trigger_for_event,
)
from orchestrator.core.schemas import TaskCreate
from orchestrator.services import (
    artifact_service,
    log_service,
    notification_service,
    settings_service,
    task_service,
)

SECRET = "sk-ant-api03-supersecretvalue1234567890"


@pytest.fixture(autouse=True)
def _reset_coalesce_timer(monkeypatch):
    monkeypatch.setattr(notification_service, "_timer", None)


def _boom(*_a, **_k):
    raise RuntimeError("exploded")


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)

    def fake_deliver(title: str, body: str, link: str = "") -> notification_service.TransportResult:
        calls.append({"title": title, "body": body, "link": link})
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel", None, "plain")

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    return calls


def _task_run(session, title="notification task", run_id="R-1"):
    task = task_service.create(
        session, TaskCreate(title=title, phase_number=1, phase_slug="notify")
    )
    run = Run(
        short_id=run_id, task_id=task.id, status="completed", exit_code=0, duration_ms=120_000
    )
    session.add(run)
    session.commit()
    session.refresh(task)
    session.refresh(run)
    return task, run


def _rows(session):
    session.expire_all()
    return list(session.exec(select(Notification).order_by(col(Notification.id))))


def _log(session, event, task, run=None, details="done"):
    return log_service.log_event(
        session, event, details, role="worker", task_id=task.id, run_id=getattr(run, "id", None)
    )


def test_off_by_default_records_skip_and_delivers_nothing(session, sent):
    task, run = _task_run(session)
    assert settings_service.defaults()[MASTER_SETTING] is False
    assert settings_service.defaults()["notification_run_completed"] is True
    assert settings_service.defaults()["notification_worktree_created"] is False
    _log(session, "run_completed", task, run)
    assert sent == []
    assert [(r.status, r.reason) for r in _rows(session)] == [("skipped", "master_disabled")]


def test_trigger_selection_against_the_real_event_vocabulary():
    required = {
        "run_completed",
        "run_failed",
        "run_timeout",
        "run_cancelled",
        "escalated",
        "merge_candidate",
        "merged",
        "merge_refused",
    }
    assert required <= DIRECT_EVENTS
    assert all(trigger_for_event(k).key == k for k in required)  # type: ignore[union-attr]
    src, dst = TaskStatus.in_progress.value, TaskStatus.review.value
    assert trigger_for_event("task_status", f"{src} -> {dst}: handoff").key == "task_review"  # type: ignore[union-attr]
    assert (
        trigger_for_event("task_status", f"{src} -> {TaskStatus.blocked.value}: x").key
        == "task_blocked"
    )  # type: ignore[union-attr]
    # Tied to the real transition format, not a substring: these must not fire.
    assert trigger_for_event("task_status", f"{src} -> {dst}_notes: x") is None
    assert trigger_for_event("task_status", f"note: {src} -> {dst}") is None
    assert trigger_for_event("task_status", f"{src} -> {TaskStatus.accepted.value}: x") is None
    assert trigger_for_event("task_created", "T-1: created") is None
    assert set(TRIGGERS) == set(CORE_TRIGGERS) | set(NOISY_TRIGGERS)


def test_payload_shape_link_and_redaction(session, sent):
    task, run = _task_run(session, title=f"fix {SECRET} leak")
    settings_service.update(session, {MASTER_SETTING: True})
    _log(session, "run_completed", task, run, f"{run.short_id} finished; token={SECRET}")
    title, body, link = sent[0]["title"], sent[0]["body"], sent[0]["link"]
    assert task.short_id in title and "Run completed" in title
    assert f"Run: {run.short_id} (completed)" in body
    assert "Outcome: run finished normally (exit 0)" in body
    assert "Next: Review the handoff" in body
    assert link == f"http://127.0.0.1:8321/tasks/{task.id}" and f"Open: {link}" in body
    assert SECRET not in title + body + link and "[REDACTED]" in title and "[REDACTED]" in body
    assert [(_rows(session)[0].status, _rows(session)[0].channel)] == [
        ("delivered", "test-channel")
    ]


def test_parallel_runs_coalesce_and_a_trigger_is_deduped(session, sent, monkeypatch):
    task1, run1 = _task_run(session, "first", "R-1")
    task2, run2 = _task_run(session, "second", "R-2")
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    _log(session, "run_completed", task1, run1)
    _log(session, "run_completed", task2, run2)
    _log(
        session,
        "task_status",
        task1,
        details=f"{TaskStatus.in_progress.value} -> {TaskStatus.review.value}: x",
    )
    _log(session, "run_completed", task1, run1, "again")  # same run+trigger: deduped
    assert sent == []
    notification_service.flush_pending(session)
    assert len(sent) == 1 and all(
        t in sent[0]["body"] for t in ("first", "second", "[run_completed]", "[task_review]")
    )
    rows = _rows(session)
    assert [r.status for r in rows] == ["delivered", "coalesced", "coalesced"]
    assert rows[1].coalesced_into == rows[0].id == rows[2].coalesced_into


def test_failures_never_lose_the_build_log_or_poison_the_session(session, monkeypatch):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)

    def channel_boom(*_a, **_k):
        raise RuntimeError("desktop channel exploded")

    monkeypatch.setattr(notification_service, "deliver", channel_boom)
    entry = _log(session, "run_failed", task, run, "failed")
    assert session.get(BuildLogEntry, entry.id) is not None and session.is_active
    row = _rows(session)[0]
    assert (row.status, "desktop channel exploded" in (row.error or "")) == ("failed", True)
    session.refresh(task)
    assert task.status == "planned"
    # Raising the whole fan-out still leaves the caller's session writable.
    monkeypatch.setattr(notification_service, "notify_event", _boom)
    entry2 = _log(session, "run_completed", task, run)
    assert session.get(BuildLogEntry, entry2.id) is not None and session.is_active
    session.add(BuildLogEntry(event="after_failure", details="still writable"))
    session.commit()


def test_a_bug_inside_the_fanout_leaves_one_durable_failed_row(session, monkeypatch):
    task, run = _task_run(session)
    monkeypatch.setattr(notification_service, "trigger_for_event", _boom)
    notification_service.notify_event(event="run_completed", task_id=task.id, run_id=run.id)
    assert [(r.status, r.reason, r.trigger) for r in _rows(session)] == [
        ("failed", "fanout_error", "unknown")
    ]


def test_noisy_events_are_silent_until_enabled(session, sent):
    task, run1 = _task_run(session, run_id="R-1")
    settings_service.update(session, {MASTER_SETTING: True})
    _log(session, "worktree_created", task, run1, "/tmp/wt")
    assert sent == [] and _rows(session)[0].reason == "trigger_disabled"
    settings_service.update(session, {"notification_worktree_created": True})
    run2 = Run(short_id="R-2", task_id=task.id, status="running")
    session.add(run2)
    session.commit()
    _log(session, "worktree_created", task, run2, "/tmp/wt2")
    assert len(sent) == 1 and _rows(session)[1].status == "delivered"


def test_duration_reports_compute_and_wall_and_only_names_a_large_freeze(session, sent):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    run.duration_ms = int(80.2 * 60 * 1000)
    session.add(run)
    session.commit()
    artifact_service.write_json(run.id, "run.json", {"process_ms": int(41.7 * 60 * 1000)})
    _log(session, "run_timeout", task, run, "timed out")
    assert (
        "41.7m compute / 80.2m wall" in sent[0]["body"]
        and "clock suspended 38.5m" in sent[0]["body"]
    )
    assert _rows(session)[0].details_json["duration"]["suspended_ms"] > 0
    sent.clear()
    run2 = Run(
        short_id="R-2", task_id=task.id, status="completed", exit_code=0, duration_ms=600_000
    )
    session.add(run2)
    session.commit()
    artifact_service.write_json(run2.id, "run.json", {"process_ms": 590_000})
    _log(session, "run_completed", task, run2)
    assert (
        "9.8m compute / 10.0m wall" in sent[0]["body"] and "clock suspended" not in sent[0]["body"]
    )


def test_channel_actions_markup_and_no_client(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        notification_service,
        "_run_client",
        lambda argv: (seen.append(argv), notification_service.ClientReply(argv[0], None, ""))[1],
    )
    clients = {"notify-send", "gdbus"}
    monkeypatch.setattr(
        notification_service.shutil, "which", lambda n: f"/usr/bin/{n}" if n in clients else None
    )
    monkeypatch.setattr(
        notification_service, "probe_capabilities", lambda: notification_service.UNPROBED
    )
    link = "http://127.0.0.1:8321/tasks/7"
    body = "body <b>\nOpen: " + link
    assert notification_service.deliver("title", body, link).channel == "notify-send"
    assert [a for a in seen[0] if "action" in a or a == "--wait"] == []  # no --wait trap
    assert seen[0][-1] == body  # unprobed: raw text, no escaping and no anchor
    clients = {"gdbus"}
    assert notification_service.deliver("title", "body", link).channel == "gdbus"
    assert "org.freedesktop.Notifications.Notify" in seen[1]
    assert seen[1][-3] == "[]" and seen[1][-2] == "{}"  # no action array, no markup hint
    clients.clear()
    result = notification_service.deliver("title", "body")
    assert result.status == "failed" and (result.error or "").startswith("no freedesktop")


def test_timer_is_cleared_before_delivery_and_a_missing_client_is_recorded(session, monkeypatch):
    seen = {}
    monkeypatch.setattr(notification_service, "_timer", object())
    monkeypatch.setattr(
        notification_service,
        "flush_pending",
        lambda: seen.setdefault("timer", notification_service._timer),
    )
    notification_service._flush_thread()
    assert seen["timer"] is None and notification_service._timer is None
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    monkeypatch.setattr(notification_service.shutil, "which", lambda n: None)
    _log(session, "run_completed", task, run)
    assert _rows(session)[-1].status == "failed"
    assert "no freedesktop notification client" in (_rows(session)[-1].error or "")


def test_a_failing_fanout_rolls_back_a_poisoned_caller_session(session, monkeypatch):
    """DoD 5 / review finding 1: an exception after the caller's session was used must not
    leave it in 'needs rollback' state (the run loop's next statement would die on it)."""
    task, run = _task_run(session)
    status_before = task.status
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)

    def fanout_that_poisons_the_caller_session(*, event, details="", task_id=None, run_id=None):
        # Exactly the shape of the bug: a write into the CALLER's transaction that fails on
        # flush (NOT NULL), which leaves the session requiring rollback, then a raise.
        session.add(BuildLogEntry(event=None, details="half-written"))
        session.flush()
        raise RuntimeError("never reached")

    monkeypatch.setattr(
        notification_service, "notify_event", fanout_that_poisons_the_caller_session
    )
    entry = log_service.log_event(session, "run_completed", "done", task_id=task.id, run_id=run.id)
    assert session.is_active  # mutation check: without session.rollback() this is False
    assert session.get(BuildLogEntry, entry.id) is not None  # the build-log line survived
    session.add(BuildLogEntry(event="after_poison", details="still writable"))
    session.commit()
    session.refresh(task)
    assert task.status == status_before  # a notification failure never touches task state
    # Recorded, not swallowed: exactly one durable failed row naming the fan-out error.
    failures = [r for r in _rows(session) if r.reason == "fanout_error"]
    assert len(failures) == 1
    assert "server_default_UUID" not in (failures[0].error or "")  # no stale-state noise
    assert "NOT NULL constraint failed" in (failures[0].error or "")


def test_an_event_arriving_mid_flush_rearms_the_timer(session, monkeypatch):
    """Review finding 2: the coalescing timer must be cleared BEFORE delivery, so an event
    that lands while a notification is being delivered is never left pending forever."""
    task, run = _task_run(session)
    task2, run2 = _task_run(session, "second", "R-2")

    class FakeTimer:
        instances: list[FakeTimer] = []

        def __init__(self, interval, fn):
            self.interval, self.fn, self.daemon, self.started = interval, fn, False, False
            FakeTimer.instances.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service.threading, "Timer", FakeTimer)
    settings_service.update(session, {MASTER_SETTING: True})
    _log(session, "run_completed", task, run)
    assert [t.started for t in FakeTimer.instances] == [True]
    timer_while_delivering = {}

    def deliver_and_race(title, body, link=""):
        # A parallel run finishes while this delivery is in flight.
        timer_while_delivering["timer"] = notification_service._timer
        _log(session, "run_completed", task2, run2)
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel"
        )

    monkeypatch.setattr(notification_service, "deliver", deliver_and_race)
    notification_service._flush_thread()
    assert timer_while_delivering["timer"] is None  # cleared before delivery
    assert len(FakeTimer.instances) == 2  # the mid-flush event re-armed the timer
    assert [r.status for r in _rows(session)] == ["delivered", "pending"]


def test_standalone_run_trigger_is_subscribed_by_default_and_maps_directly():
    trigger = trigger_for_event("standalone_run")
    assert trigger is not None and trigger.key == "standalone_run"
    assert not trigger.noisy and trigger.key in CORE_TRIGGERS
    assert trigger.setting == "notification_standalone_run"
    assert settings_service.defaults()["notification_standalone_run"] is True


def _standalone_run(
    session, tmp_path, *, status=StandaloneStatus.completed, title="[reporter] summarise",
    agent_name="reporter",
):
    agent = Agent(name=agent_name, instructions="")
    session.add(agent)
    session.commit()
    session.refresh(agent)
    report = tmp_path / "reports" / "2026-11-05-1200-run7.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# report\n", encoding="utf-8")
    run = StandaloneRun(
        agent_id=agent.id or 0,
        title=title,
        status=status,
        report_path=str(report) if status == StandaloneStatus.completed else None,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run, report


def test_a_completed_standalone_run_writes_one_row_naming_agent_and_report(
    session, sent, tmp_path
):
    run, report = _standalone_run(session, tmp_path)
    settings_service.update(session, {MASTER_SETTING: True})
    notification_service.notify_event(
        event="standalone_run", details=f"{run.title}: completed", standalone_run_id=run.id
    )
    rows = _rows(session)
    assert len(rows) == 1
    assert rows[0].trigger == "standalone_run" and rows[0].status == "delivered"
    assert rows[0].run_id is None and rows[0].task_id is None
    assert rows[0].dedupe_key == f"standalone:{run.id}:standalone_run"
    assert "reporter" in sent[0]["title"]
    assert "standalone #" in sent[0]["body"] and "(completed)" in sent[0]["body"]
    assert report.name in sent[0]["body"]
    assert sent[0]["link"].endswith("/agents") and "/tasks/" not in sent[0]["link"]
    # Same run, same trigger: at most one record, exactly as for task runs.
    notification_service.notify_event(
        event="standalone_run", details="again", standalone_run_id=run.id
    )
    assert len(_rows(session)) == 1


def test_standalone_trigger_off_delivers_nothing(session, sent, tmp_path):
    run, _ = _standalone_run(session, tmp_path)
    settings_service.update(
        session, {MASTER_SETTING: True, "notification_standalone_run": False}
    )
    notification_service.notify_event(
        event="standalone_run", details="done", standalone_run_id=run.id
    )
    assert sent == []
    row = _rows(session)[0]
    assert (row.trigger, row.status, row.reason) == (
        "standalone_run",
        "skipped",
        "trigger_disabled",
    )


def test_a_skipped_standalone_run_is_reported_as_skipped(session, sent, tmp_path):
    run, _ = _standalone_run(session, tmp_path, status=StandaloneStatus.skipped)
    settings_service.update(session, {MASTER_SETTING: True})
    notification_service.notify_event(
        event="standalone_run",
        details=f"{run.title}: skipped (standalone runs are off)",
        standalone_run_id=run.id,
    )
    assert "Run: standalone #" in sent[0]["body"] and "(skipped)" in sent[0]["body"]
    assert "Outcome: standalone run skipped" in sent[0]["body"]
    assert _rows(session)[0].trigger == "standalone_run"


def test_a_failed_standalone_run_reports_its_error(session, sent, tmp_path):
    run, _ = _standalone_run(session, tmp_path, status=StandaloneStatus.failed)
    run.error = "no harness here"
    session.add(run)
    session.commit()
    settings_service.update(session, {MASTER_SETTING: True})
    notification_service.notify_event(
        event="standalone_run", details=f"{run.title}: failed", standalone_run_id=run.id
    )
    assert (f"Run: standalone #{run.id} (failed)") in sent[0]["body"]
    assert "Outcome: standalone run failed: no harness here" in sent[0]["body"]
    assert "Report file: no report file saved" in sent[0]["body"]


def test_two_task_less_events_of_one_trigger_produce_two_rows(session, sent):
    settings_service.update(
        session, {MASTER_SETTING: True, "notification_schedule_fired": True}
    )
    log_service.log_event(session, "schedule_fired", "nightly -> task A")
    log_service.log_event(session, "schedule_fired", "nightly -> task B")
    rows = _rows(session)
    assert [r.trigger for r in rows] == ["schedule_fired", "schedule_fired"]
    assert len({r.dedupe_key for r in rows}) == 2
    assert all(r.dedupe_key != "task:None:schedule_fired" for r in rows)
    assert len(sent) == 2
    assert "task A" in sent[0]["body"] and "task B" in sent[1]["body"]


def test_flush_never_raises_and_never_poisons_a_caller_session(session, monkeypatch):
    """DoD 5: startup recovery runs inside the API lifespan; a dead channel must not abort it."""
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    _log(session, "run_completed", task, run)

    def commit_boom(*_a, **_k):
        session.add(BuildLogEntry(event=None, details="flush failure inside delivery"))
        session.flush()

    monkeypatch.setattr(notification_service, "_deliver_pending_locked", commit_boom)
    assert notification_service.flush_pending(session) == 0  # swallowed, not propagated
    assert session.is_active
    session.commit()


# --- T-18: truthful outcomes, capability probe and pending-row flush points -----------------

LINK = "http://127.0.0.1:8321/tasks/7"
REAL_PROBE = notification_service.probe_capabilities


def _caps(*names):
    return notification_service.DaemonCapabilities(True, frozenset(names), "GetCapabilities")


def _clients(monkeypatch, *, present=("notify-send", "gdbus"), caps=None, replies=()):
    seen, queued = [], list(replies)

    def run(argv):
        seen.append(argv)
        return queued.pop(0) if queued else notification_service.ClientReply(argv[0], None, "")

    monkeypatch.setattr(notification_service, "_run_client", run)
    monkeypatch.setattr(notification_service.shutil, "which", lambda n: n if n in present else None)
    monkeypatch.setattr(
        notification_service, "probe_capabilities", lambda: caps or notification_service.UNPROBED
    )
    return seen


def test_probe_parses_capabilities_and_fails_closed(monkeypatch):
    class Reply:
        def __init__(self, rc, out, err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr(notification_service, "probe_capabilities", REAL_PROBE)
    monkeypatch.setattr(notification_service.shutil, "which", lambda n: n)
    monkeypatch.setattr(notification_service.subprocess, "run", lambda *a, **k: Reply(
        0, "(['body', 'body-markup', 'body-hyperlinks'],)\n"))
    caps = REAL_PROBE()
    assert caps.probed and caps.supports("body-markup") and caps.supports("body-hyperlinks")
    monkeypatch.setattr(notification_service.subprocess, "run", lambda *a, **k: Reply(1, "", "no"))
    assert REAL_PROBE().probed is False

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("gdbus", 2)

    monkeypatch.setattr(notification_service.subprocess, "run", boom)
    assert REAL_PROBE().probed is False
    monkeypatch.setattr(notification_service.shutil, "which", lambda n: None)
    assert REAL_PROBE().source == "gdbus not on PATH"


def test_markup_daemon_gets_escaped_text_on_every_transport_and_no_hint(monkeypatch):
    body = '<b>x</b> & "q"\nOpen: ' + LINK
    seen = _clients(monkeypatch, present={"notify-send"}, caps=_caps("body-markup", "body-hyperlinks"))
    result = notification_service.deliver("t", body, LINK)
    assert (result.shape, result.channel) == ("markup-escaped", "notify-send")
    assert seen[0][-1] == '&lt;b&gt;x&lt;/b&gt; &amp; "q"\nOpen: ' + LINK
    seen = _clients(monkeypatch, present={"gdbus"}, caps=_caps("body-markup"),
                    replies=[notification_service.ClientReply("gdbus", None, "(uint32 7,)\n")])
    result = notification_service.deliver("t", body, LINK)
    assert (result.shape, result.status) == ("markup-escaped", "delivered") and seen[0][-2] == "{}"
    seen = _clients(monkeypatch, present={"gdbus"}, caps=_caps("body-markup", "body-hyperlinks"),
                    replies=[notification_service.ClientReply("gdbus", None, "(uint32 8,)\n")])
    assert notification_service.deliver("t", body, LINK).shape == "markup+hyperlink"
    assert seen[0][-2] == "{}"
    assert '<a href="' + LINK + '">Open task</a>' in json.loads(seen[0][-4])
    seen = _clients(monkeypatch, present={"notify-send"}, caps=notification_service.UNPROBED)
    notification_service.deliver("t", "raw <b>")
    assert seen[0][-1] == "raw <b>"  # an unprobed daemon is not assumed to parse markup


@pytest.mark.parametrize("present,replies,expected", [
    ({"notify-send"}, [], ("delivered", "shown_unacknowledged")),
    ({"gdbus"}, [notification_service.ClientReply("gdbus", None, "(uint32 42,)\n")],
     ("delivered", "")),
    ({"notify-send"}, [notification_service.ClientReply("notify-send", "exit 1")],
     ("failed", "delivery_failed")),
    # A client that was launched but never reported back may well have shown the message:
    # the old --action/--wait trap produced exactly this shape, so it is not "failed".
    ({"notify-send"}, [notification_service.ClientReply("notify-send", "timed out after 10s",
                                                         timed_out=True)],
     ("delivered", "shown_unacknowledged")),
    (set(), [], ("failed", "delivery_failed")),
])
def test_every_transport_result_maps_to_one_frozen_status(
    monkeypatch, present, replies, expected
):
    assert set(notification_service.STATUS_FOR_OUTCOME) == set(notification_service.OUTCOME_CLASSES)
    assert set(notification_service.STATUS_FOR_OUTCOME.values()) <= {
        "pending", "delivered", "failed", "skipped", "coalesced"}
    _clients(monkeypatch, present=present, replies=replies)
    result = notification_service.deliver("t", "b", LINK)
    assert (result.status, result.reason) == expected


def test_a_displayed_message_is_recorded_delivered_not_failed(session, monkeypatch):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    _clients(monkeypatch, present={"notify-send"})
    _log(session, "run_timeout", task, run, "timed out")
    row = _rows(session)[0]
    assert (row.status, row.reason, row.error) == ("delivered", "shown_unacknowledged", None)


def test_a_client_timeout_after_spawn_records_the_unknown_display_caveat(session, monkeypatch):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    _clients(monkeypatch, present={"notify-send"},
             replies=[notification_service.ClientReply("notify-send", "timed out after 10s",
                                                       timed_out=True)])
    _log(session, "run_timeout", task, run, "timed out")
    row = _rows(session)[0]
    assert (row.status, row.reason, row.error) == ("delivered", "shown_unacknowledged", None)
    assert "display unconfirmed" in row.details_json["delivery_note"]


@pytest.mark.parametrize("case,present,replies,master,expected", [
    ("delivered", {"gdbus"}, [notification_service.ClientReply("gdbus", None, "(uint32 7,)\n")],
     True, ("delivered", "")),
    ("shown", {"notify-send"}, [], True, ("delivered", "shown_unacknowledged")),
    ("failed", set(), [], True, ("failed", "delivery_failed")),
    ("skipped", {"notify-send"}, [], False, ("skipped", "master_disabled")),
])
def test_the_standalone_path_reaches_every_outcome_class(
    session, monkeypatch, tmp_path, case, present, replies, master, expected
):
    """DoD 1: a standalone_run delivery must land in every class; its payload differs only."""
    run, _ = _standalone_run(session, tmp_path)
    settings_service.update(session, {MASTER_SETTING: master})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    seen = _clients(monkeypatch, present=present, replies=replies)
    notification_service.notify_event(event="standalone_run", details=f"{run.title}: completed",
                                      standalone_run_id=run.id)
    row = _rows(session)[0]
    assert (row.status, row.reason) == expected
    assert row.link.endswith("/agents")
    assert len(seen) == (1 if case in {"delivered", "shown"} else 0)


def test_two_standalone_runs_coalesce_into_one_message(session, monkeypatch, tmp_path):
    first, _ = _standalone_run(session, tmp_path)
    second, _ = _standalone_run(session, tmp_path, title="[reporter] audit", agent_name="auditor")
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    calls = []

    def fake_deliver(title, body, link=""):
        calls.append((title, body))
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel")

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    for run in (first, second):
        notification_service.notify_event(event="standalone_run",
                                          details=f"{run.title}: completed",
                                          standalone_run_id=run.id)
    assert [r.status for r in _rows(session)] == ["pending", "pending"]
    notification_service.flush_pending(session)
    rows = _rows(session)
    assert [r.status for r in rows] == ["delivered", "coalesced"]
    assert rows[1].coalesced_into == rows[0].id
    assert len(calls) == 1 and calls[0][0] == "Coding Orchestrator — 2 updates"
    assert "[reporter] audit" in calls[0][1]


def test_two_runs_finishing_together_produce_one_message(session, monkeypatch):
    """DoD 4: coalescing crosses runs, so parallel finishes are one message, not a stampede."""
    task1, run1 = _task_run(session, "first", "R-1")
    task2, run2 = _task_run(session, "second", "R-2")
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    calls = []

    def fake_deliver(title, body, link=""):
        calls.append((title, body))
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel")

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    _log(session, "run_completed", task1, run1)
    _log(session, "run_completed", task2, run2)
    notification_service.flush_pending(session)
    rows = _rows(session)
    assert (len(calls), calls[0][0]) == (1, "Coding Orchestrator — 2 updates")
    assert [r.status for r in rows] == ["delivered", "coalesced"]
    assert (rows[0].run_id, rows[1].run_id) == (run1.id, run2.id)  # different runs
    assert rows[1].coalesced_into == rows[0].id


def _pending(session, key, owner=None, title="pending row"):
    row = Notification(trigger="run_completed", event="run_completed", status="pending",
                       title=title, body="from a dead CLI timer", dedupe_key=key,
                       details_json={notification_service.OWNER_KEY:
                                     owner or notification_service.PROCESS_TOKEN})
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_exit_flush_delivers_and_terminates_only_real_rows_it_owns(session, monkeypatch):
    """DoD 3: a real `notify_event` row carries this process's token, and only it is terminated."""
    _task, run = _task_run(session)
    other = _pending(session, "server:real", owner="server:pid")
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda: None)
    assert notification_service.notify_event(event="run_completed", run_id=run.id) is not None
    row = [r for r in _rows(session) if r.dedupe_key == f"{run.id}:run_completed"][0]
    assert row.status == "pending"
    assert row.details_json[notification_service.OWNER_KEY] == notification_service.PROCESS_TOKEN
    monkeypatch.setattr(notification_service, "flush_pending",
                        lambda session=None, owner=None: 0)
    assert notification_service.flush_pending_at_exit(session) == 1
    assert (row.status, row.reason) == ("failed", "process_exit_undelivered")
    assert other.status == "pending"


def test_exit_flush_is_inert_before_the_database_exists(home):
    assert notification_service.flush_pending_at_exit() == 0
    assert not (home / "orchestrator.db").exists()


def test_cli_exit_never_delivers_a_row_the_server_created(session, monkeypatch):
    """DoD 3 + cross-process safety: the exit flush is scoped to rows this process created."""
    ours = _pending(session, "cli:own", title="cli own")
    theirs = _pending(session, "server:own", owner="server:pid", title="server own")
    calls = []

    def fake_deliver(title, body, link=""):
        calls.append(title)
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel")

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    notification_service.flush_pending_at_exit(session)
    rows = {r.dedupe_key: r for r in _rows(session)}
    assert calls == ["cli own"]
    assert (rows["cli:own"].status, rows["cli:own"].id) == ("delivered", ours.id)
    assert (rows["server:own"].status, rows["server:own"].id) == ("pending", theirs.id)


def test_two_flushers_race_and_only_the_claim_winner_delivers(session, monkeypatch):
    """DoD 4: a CLI exit and a server timer must not deliver the same row twice."""
    from orchestrator.core.db import session_scope

    row = _pending(session, "race:1", title="race row")
    calls, other_results = [], []

    def fake_deliver(title, body, link=""):
        calls.append(title)
        # Another process's flusher runs while this one is delivering.
        with session_scope() as other:
            other_results.append(notification_service._deliver_pending_locked(other))
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel")

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    assert notification_service.flush_pending(session) == row.id
    session.expire_all()
    assert calls == ["race row"]
    assert other_results == [0]  # the racing flusher claimed nothing: no second popup
    assert [(r.id, r.status) for r in _rows(session)] == [(row.id, "delivered")]


def test_a_dead_flushers_claim_is_released_instead_of_stranding_the_row(session, monkeypatch):
    """DoD 3: a process that claimed a row and died must not leave it pending forever."""
    row = _pending(session, "stale:1", title="stale row")
    row.reason = (f"{notification_service.CLAIM_PREFIX}dead:abc:"
                  f"{int(time.time()) - notification_service.CLAIM_STALE_SECONDS - 60}")
    session.add(row)
    session.commit()
    calls = []

    def fake_deliver(title, body, link=""):
        calls.append(title)
        return notification_service.TransportResult(
            notification_service.OUTCOME_DELIVERED, "test-channel")

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    notification_service.flush_pending(session)
    assert calls == ["stale row"]
    delivered = _rows(session)[0]
    assert (delivered.status, delivered.reason) == ("delivered", "")
