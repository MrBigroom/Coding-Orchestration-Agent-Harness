"""Fire-and-forget desktop notifications for events that need a human (T-9).

`log_service.log_event` calls this after committing the build-log row, so every emitter is
covered without touching the state machine. Missing D-Bus, an absent client, a dead server or a
raising notifier is recorded, never propagated. Delivery is best-effort; the `notifications`
table is the durable record (`GET /api/notifications`, GUI Notifications page).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from contextlib import nullcontext
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import Notification, Run, Task
from orchestrator.core.notification_vocab import (
    MASTER_SETTING,
    NotificationTrigger,
    trigger_for_event,
)
from orchestrator.services import artifact_service, settings_service
from orchestrator.utils.redaction import redact

# Let a run's back-to-back events (review, merge candidate, completed) become one popup.
COALESCE_SECONDS = 2.0
DELIVERY_TIMEOUT_SECONDS = 10

PENDING = "pending"
DELIVERED = "delivered"
FAILED = "failed"
SKIPPED = "skipped"
COALESCED = "coalesced"

_timers: dict[str, threading.Timer] = {}
_lock = threading.Lock()


def _human_ms(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    seconds = float(ms) / 1000.0
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    return f"{minutes:.1f}m" if minutes < 100 else f"{minutes:.0f}m"


def duration_details(run: Run | None) -> dict[str, Any]:
    """Compute vs wall time, read from `run.json.process_ms` (no new persisted field)."""
    if run is None:
        return {}
    wall = run.duration_ms
    archived = artifact_service.read_json(run.id, "run.json") if run.id is not None else None
    compute = (archived or {}).get("process_ms")
    try:
        wall_ms = int(wall) if wall is not None else None
    except (TypeError, ValueError):
        wall_ms = None
    try:
        compute_ms = int(compute) if compute is not None else None
    except (TypeError, ValueError):
        compute_ms = None
    out: dict[str, Any] = {"compute_ms": compute_ms, "wall_ms": wall_ms}
    if compute_ms is not None and wall_ms is not None and wall_ms > 0:
        gap = wall_ms - compute_ms
        text = f"{_human_ms(compute_ms)} compute / {_human_ms(wall_ms)} wall"
        if gap > 0.25 * wall_ms:
            out["suspended_ms"] = gap
            text += f"; clock suspended {_human_ms(gap)}"
        out["text"] = text
    elif wall_ms is not None:
        out["text"] = f"{_human_ms(wall_ms)} wall (compute window not archived)"
    elif compute_ms is not None:
        out["text"] = f"{_human_ms(compute_ms)} compute (wall duration not recorded)"
    return out


def _resolve_run(session: Session, task_id: int | None, run_id: int | None) -> Run | None:
    if run_id is not None:
        return session.get(Run, run_id)
    if task_id is None:
        return None
    return session.exec(
        select(Run).where(Run.task_id == task_id).order_by(col(Run.id).desc())
    ).first()


def _task_link(task: Task | None) -> str:
    if task is None or task.id is None:
        return ""
    s = get_settings()
    host = s.bind_host if s.bind_host not in ("0.0.0.0", "::") else "127.0.0.1"  # noqa: S104
    return f"http://{host}:{s.bind_port}/tasks/{task.id}"


def _outcome(trigger: NotificationTrigger, run: Run | None, details: str) -> str:
    key = trigger.key
    if key == "run_completed":
        return f"run finished normally (exit {run.exit_code if run else 'unknown'})"
    if key == "run_failed":
        err = f": {run.error}" if run and run.error else ""
        return f"run failed (exit {run.exit_code if run else 'unknown'}){err}"
    if key == "run_timeout":
        err = f": {run.error}" if run and run.error else ""
        return f"run hit the adapter timeout{err}"
    if key == "run_cancelled":
        return f"run cancelled (exit {run.exit_code if run else 'unknown'})"
    if key == "task_review":
        return "task is in review and waiting for a human decision"
    if key == "task_blocked":
        reason = details.split(":", 1)[1].strip() if ":" in details else details.strip()
        return "task is blocked" + (f": {reason}" if reason else "")
    if key == "escalated":
        return "task was escalated to an Architect review"
    if key == "merge_candidate":
        return "accepted; merge gate off, waiting for a manual merge"
    if key == "merged":
        return "accepted phase branch merged with --no-ff"
    if key == "merge_refused":
        return "the merge gate refused this branch"
    return trigger.label.lower()


def _payload(
    trigger: NotificationTrigger, task: Task | None, run: Run | None, details: str
) -> dict[str, Any]:
    label = f"{task.short_id} {task.title}" if task else "orchestrator"
    outcome = _outcome(trigger, run, details)
    lines = [
        f"Task: {label}",
        f"Run: {run.short_id if run else 'no run'} ({run.status if run else 'n/a'})",
        f"Outcome: {outcome}",
        f"Next: {trigger.action}",
    ]
    duration = duration_details(run) if trigger.terminal_run else {}
    if duration.get("text"):
        lines.append(f"Duration: {duration['text']}")
    link = _task_link(task)
    if link:
        lines.append(f"Open: {link}")
    detail = " ".join((details or "").split())
    if detail:
        lines.append(f"Detail: {detail[:400]}")
    return {
        "title": redact(f"{label} — {trigger.label}"),
        "body": redact("\n".join(lines)),
        "link": redact(link),
        "details": {
            "trigger": trigger.key,
            "outcome": redact(outcome),
            "duration": duration,
            "detail": redact(detail[:400]),
        },
    }


def _notification_env() -> dict[str, str]:
    """Selected session vars only: never pass provider keys to a notification client."""
    names = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def _run_client(argv: list[str]) -> tuple[str, str | None]:
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            argv,
            env=_notification_env(),
            capture_output=True,
            text=True,
            timeout=DELIVERY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return argv[0], f"timed out after {DELIVERY_TIMEOUT_SECONDS}s"
    except OSError as e:
        return argv[0], redact(str(e))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return argv[0], redact(err[-500:] or f"exit {proc.returncode}")
    return argv[0], None


def deliver(title: str, body: str, link: str = "") -> tuple[str | None, str | None]:
    """Send one desktop notification. Returns `(channel, error)`."""
    if shutil.which("notify-send"):
        return _run_client(
            [
                "notify-send",
                "--app-name=Coding Orchestrator",
                "--urgency=normal",
                "--icon=dialog-information",
                title,
                body,
            ]
        )
    if shutil.which("gdbus"):
        # Direct freedesktop Notifications message: no GUI toolkit or window required.
        return _run_client(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.Notifications",
                "--object-path",
                "/org/freedesktop/Notifications",
                "--method",
                "org.freedesktop.Notifications.Notify",
                json.dumps("Coding Orchestrator"),
                "0",
                json.dumps(""),
                json.dumps(title),
                json.dumps(body),
                "[]",
                "{}",
                "10000",
            ]
        )
    return None, "no freedesktop notification client on PATH (notify-send/gdbus)"


def _coalesce_key(run_id: int | None, task_id: int | None) -> str:
    if run_id is not None:
        return f"run:{run_id}"
    return f"task:{task_id}" if task_id is not None else "global"


def _flush(coalesce_key: str, session: Session | None = None) -> int:
    """Deliver all pending rows for one key as one notification, recording the outcome."""
    from orchestrator.core.db import session_scope

    ctx = nullcontext(session) if session is not None else session_scope()
    with ctx as s:
        rows = list(
            s.exec(
                select(Notification)
                .where(
                    Notification.coalesce_key == coalesce_key,
                    Notification.status == PENDING,
                )
                .order_by(col(Notification.id))
            )
        )
        if not rows:
            return 0
        first = rows[0]
        if len(rows) == 1:
            title, body = first.title, first.body
        else:
            base = first.title.split(" — ", 1)[0]
            triggers = ", ".join(dict.fromkeys(r.trigger for r in rows))
            title = redact(f"{base} — {len(rows)} updates ({triggers})")
            body = redact("\n\n---\n\n".join(f"[{r.trigger}]\n{r.body}" for r in rows))
        try:
            channel, error = deliver(title, body, first.link)
        except Exception as e:  # a notifier bug is recorded, never propagated
            channel, error = None, redact(str(e))[:1000]
        first.title, first.body, first.channel, first.error = title, body, channel, error
        first.status = DELIVERED if channel and error is None else FAILED
        if first.status == FAILED:
            first.reason = "delivery_failed"
        for extra in rows[1:]:
            extra.status = COALESCED
            extra.coalesced_into = first.id
            extra.channel = channel
            extra.reason = f"coalesced_into:{first.id}"
        for row in rows:
            s.add(row)
        s.commit()
        return first.id or 0


def _flush_from_thread(coalesce_key: str) -> None:
    try:
        _flush(coalesce_key)
    except Exception:  # noqa: BLE001, S110 - the timer thread must never raise
        pass
    finally:
        with _lock:
            _timers.pop(coalesce_key, None)


def _schedule(coalesce_key: str) -> None:
    with _lock:
        if coalesce_key in _timers:
            return
        timer = threading.Timer(COALESCE_SECONDS, _flush_from_thread, args=(coalesce_key,))
        timer.daemon = True
        _timers[coalesce_key] = timer
        timer.start()


def flush_pending(session: Session, coalesce_key: str | None = None) -> int:
    """Deliver pending rows now (tests and startup recovery). Returns rows flushed."""
    q = select(Notification).where(Notification.status == PENDING)
    if coalesce_key is not None:
        q = q.where(Notification.coalesce_key == coalesce_key)
    keys = list(dict.fromkeys(n.coalesce_key for n in session.exec(q).all()))
    return sum(1 if _flush(key, session) else 0 for key in keys)


def notify_event(
    session: Session,
    *,
    event: str,
    details: str = "",
    task_id: int | None = None,
    run_id: int | None = None,
) -> Notification | None:
    """Fan one existing build-log event out to the desktop and persistent list."""
    try:
        trigger = trigger_for_event(event, details)
        if trigger is None:
            return None
        task = session.get(Task, task_id) if task_id else None
        run = _resolve_run(session, task_id, run_id)
        if task is None and run is not None:
            task = session.get(Task, run.task_id)
        resolved_task_id = task.id if task is not None else task_id
        resolved_run_id = run.id if run is not None else run_id
        coalesce_key = _coalesce_key(resolved_run_id, resolved_task_id)
        dedupe_key = f"{coalesce_key}:{trigger.key}"
        existing = session.exec(
            select(Notification).where(Notification.dedupe_key == dedupe_key)
        ).first()
        if existing is not None:
            return existing
        payload = _payload(trigger, task, run, details)
        master = bool(settings_service.get(session, MASTER_SETTING))
        subscribed = bool(settings_service.get(session, trigger.setting))
        values = {
            "task_id": resolved_task_id,
            "run_id": resolved_run_id,
            "trigger": trigger.key,
            "event": event,
            "title": payload["title"],
            "body": payload["body"],
            "link": payload["link"],
            "dedupe_key": dedupe_key,
            "coalesce_key": coalesce_key,
            "details_json": payload["details"],
        }
        if not master or not subscribed:
            row = Notification(
                **values,
                status=SKIPPED,
                reason="master_disabled" if not master else "trigger_disabled",
            )
            session.add(row)
            session.commit()
            return row
        row = Notification(**values, status=PENDING)
        session.add(row)
        session.commit()
        if COALESCE_SECONDS <= 0:
            _flush(coalesce_key, session)
        else:
            _schedule(coalesce_key)
        return row
    except Exception:  # noqa: BLE001 - notification is best-effort by contract
        return None
