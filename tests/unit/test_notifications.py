"""T-9: triggers, payload/redaction, coalescing, channel choice, failure isolation, duration."""

from __future__ import annotations

import pytest
from sqlmodel import col, select

from orchestrator.core.models import BuildLogEntry, Notification, Run
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


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)

    def fake_deliver(title: str, body: str, link: str = "") -> tuple[str, None]:
        calls.append({"title": title, "body": body, "link": link})
        return "test-channel", None

    monkeypatch.setattr(notification_service, "deliver", fake_deliver)
    return calls


def _task_run(session, title: str = "notification task", run_id: str = "R-1"):
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


def _rows(session) -> list[Notification]:
    return list(session.exec(select(Notification).order_by(col(Notification.id))))


def test_off_by_default_records_skip_and_delivers_nothing(session, sent):
    task, run = _task_run(session)
    assert settings_service.defaults()[MASTER_SETTING] is False
    assert settings_service.defaults()["notification_run_completed"] is True
    assert settings_service.defaults()["notification_worktree_created"] is False
    log_service.log_event(
        session, "run_completed", "done", role="worker", task_id=task.id, run_id=run.id
    )
    assert sent == []
    assert _rows(session)[0].status == "skipped"
    assert _rows(session)[0].reason == "master_disabled"


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
    for key in required:
        assert trigger_for_event(key).key == key  # type: ignore[union-attr]
    assert trigger_for_event("task_status", "in_progress -> review: x").key == "task_review"  # type: ignore[union-attr]
    assert trigger_for_event("task_status", "in_progress -> blocked: x").key == "task_blocked"  # type: ignore[union-attr]
    assert trigger_for_event("task_status", "queued -> in_progress: claimed") is None
    assert trigger_for_event("task_created", "T-1: created") is None
    assert set(TRIGGERS) == set(CORE_TRIGGERS) | set(NOISY_TRIGGERS)
    assert all(trigger_for_event(k) for k in NOISY_TRIGGERS)


def test_payload_shape_link_and_redaction(session, sent):
    task, run = _task_run(session, title=f"fix {SECRET} leak")
    settings_service.update(session, {MASTER_SETTING: True})
    log_service.log_event(
        session,
        "run_completed",
        f"{run.short_id} finished; token={SECRET}",
        role="worker",
        task_id=task.id,
        run_id=run.id,
    )
    title, body = sent[0]["title"], sent[0]["body"]
    assert task.short_id in title and "Run completed" in title
    assert f"Run: {run.short_id} (completed)" in body
    assert "Outcome: run finished normally (exit 0)" in body
    assert "Next: Review the handoff" in body
    assert f"http://127.0.0.1:8321/tasks/{task.id}" in body
    assert SECRET not in title and SECRET not in body
    assert "[REDACTED]" in title and "[REDACTED]" in body
    row = _rows(session)[0]
    assert row.status == "delivered" and row.channel == "test-channel"
    assert SECRET not in row.title + row.body + row.link


def test_burst_for_one_run_is_coalesced_but_each_trigger_keeps_a_row(session, sent, monkeypatch):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 60.0)
    monkeypatch.setattr(notification_service, "_schedule", lambda key: None)
    log_service.log_event(
        session, "run_completed", "done", role="worker", task_id=task.id, run_id=run.id
    )
    log_service.log_event(
        session, "task_status", "in_progress -> review: handoff received", task_id=task.id
    )
    log_service.log_event(
        session, "merge_candidate", "merge gate disabled", task_id=task.id, run_id=run.id
    )
    assert sent == []
    notification_service.flush_pending(session)
    assert len(sent) == 1
    for trigger in ("run_completed", "task_review", "merge_candidate"):
        assert f"[{trigger}]" in sent[0]["body"]
    rows = _rows(session)
    assert [r.status for r in rows] == ["delivered", "coalesced", "coalesced"]
    assert rows[1].coalesced_into == rows[0].id == rows[2].coalesced_into


def test_raising_notifier_records_failure_and_build_log_survives(session, monkeypatch):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)

    def boom(*_args, **_kwargs):
        raise RuntimeError("desktop channel exploded")

    monkeypatch.setattr(notification_service, "deliver", boom)
    entry = log_service.log_event(
        session, "run_failed", "failed", role="worker", task_id=task.id, run_id=run.id
    )
    assert session.get(BuildLogEntry, entry.id) is not None
    session.refresh(task)
    assert task.status == "planned"
    row = _rows(session)[0]
    assert row.status == "failed" and "desktop channel exploded" in (row.error or "")


def test_raising_fanout_never_loses_the_build_log(session, monkeypatch):
    task, run = _task_run(session)

    def boom(*_args, **_kwargs):
        raise RuntimeError("fan-out exploded")

    monkeypatch.setattr(notification_service, "notify_event", boom)
    entry = log_service.log_event(
        session, "run_completed", "done", role="worker", task_id=task.id, run_id=run.id
    )
    assert session.get(BuildLogEntry, entry.id) is not None


def test_noisy_events_are_silent_until_enabled(session, sent):
    task, run1 = _task_run(session, run_id="R-1")
    settings_service.update(session, {MASTER_SETTING: True})
    log_service.log_event(
        session, "worktree_created", "/tmp/wt", role="supervisor", task_id=task.id, run_id=run1.id
    )
    assert sent == [] and _rows(session)[0].reason == "trigger_disabled"
    settings_service.update(session, {"notification_worktree_created": True})
    run2 = Run(short_id="R-2", task_id=task.id, status="running")
    session.add(run2)
    session.commit()
    log_service.log_event(
        session, "worktree_created", "/tmp/wt2", role="supervisor", task_id=task.id, run_id=run2.id
    )
    assert len(sent) == 1 and _rows(session)[1].status == "delivered"


def test_duration_reports_compute_and_wall_and_names_a_freeze(session, sent):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    run.duration_ms = int(80.2 * 60 * 1000)
    session.add(run)
    session.commit()
    artifact_service.write_json(run.id, "run.json", {"process_ms": int(41.7 * 60 * 1000)})
    log_service.log_event(
        session, "run_timeout", "timed out", role="worker", task_id=task.id, run_id=run.id
    )
    body = sent[0]["body"]
    assert "41.7m compute / 80.2m wall" in body
    assert "clock suspended 38.5m" in body
    assert _rows(session)[0].details_json["duration"]["suspended_ms"] > 0


def test_small_gap_has_no_clock_freeze_clause(session, sent):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    run.duration_ms = 600_000
    session.add(run)
    session.commit()
    artifact_service.write_json(run.id, "run.json", {"process_ms": 590_000})
    log_service.log_event(
        session, "run_completed", "done", role="worker", task_id=task.id, run_id=run.id
    )
    assert "9.8m compute / 10.0m wall" in sent[0]["body"]
    assert "clock suspended" not in sent[0]["body"]


def test_channel_prefers_notify_send_then_gdbus(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv: list[str]):
        seen.append(argv)
        return argv[0], None

    monkeypatch.setattr(notification_service, "_run_client", fake_run)
    monkeypatch.setattr(
        notification_service.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("notify-send", "gdbus") else None,
    )
    assert notification_service.deliver("title", "body") == ("notify-send", None)
    assert seen[0][0] == "notify-send"
    monkeypatch.setattr(
        notification_service.shutil,
        "which",
        lambda name: "/usr/bin/gdbus" if name == "gdbus" else None,
    )
    assert notification_service.deliver("title", "body") == ("gdbus", None)
    assert "org.freedesktop.Notifications.Notify" in seen[1]


def test_no_client_is_a_recorded_failure(session, monkeypatch):
    task, run = _task_run(session)
    settings_service.update(session, {MASTER_SETTING: True})
    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    monkeypatch.setattr(notification_service.shutil, "which", lambda name: None)
    log_service.log_event(
        session, "run_completed", "done", role="worker", task_id=task.id, run_id=run.id
    )
    row = _rows(session)[0]
    assert row.status == "failed"
    assert "no freedesktop notification client" in (row.error or "")
