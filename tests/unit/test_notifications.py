"""T-9: triggers, payload/redaction, coalescing, channel choice, failure isolation, duration."""

from __future__ import annotations

import pytest
from sqlmodel import col, select

from orchestrator.core.models import BuildLogEntry, Notification, Run, TaskStatus
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


def _boom(*_a, **_k):
    raise RuntimeError("exploded")


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)

    def fake_deliver(title: str, body: str, link: str = "") -> tuple[str, None]:
        calls.append({"title": title, "body": body, "link": link})
        return "test-channel", None

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
        notification_service, "_run_client", lambda argv: (seen.append(argv), (argv[0], None))[1]
    )
    clients = {"notify-send", "gdbus"}
    monkeypatch.setattr(
        notification_service.shutil, "which", lambda n: f"/usr/bin/{n}" if n in clients else None
    )
    link = "http://127.0.0.1:8321/tasks/7"
    assert notification_service.deliver("title", "body <b>", link) == ("notify-send", None)
    assert "--action=default=Open task" in seen[0]
    assert f'<a href="{link}">Open task</a>' in seen[0][-1] and "&lt;b&gt;" in seen[0][-1]
    clients = {"gdbus"}
    assert notification_service.deliver("title", "body", link) == ("gdbus", None)
    assert "org.freedesktop.Notifications.Notify" in seen[1]
    assert "['default', 'Open task']" in seen[1] and "{'body-markup': <true>}" in seen[1]
    clients.clear()
    assert notification_service.deliver("title", "body")[1].startswith("no freedesktop")


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
        return "test-channel", None

    monkeypatch.setattr(notification_service, "deliver", deliver_and_race)
    notification_service._flush_thread()
    assert timer_while_delivering["timer"] is None  # cleared before delivery
    assert len(FakeTimer.instances) == 2  # the mid-flush event re-armed the timer
    assert [r.status for r in _rows(session)] == ["delivered", "pending"]


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
