"""Fire-and-forget desktop notifications for events that need a human (T-9)."""

from __future__ import annotations

import html
import json
import logging
import os
import shutil
import subprocess
import threading
from contextlib import nullcontext
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.core.config import get_settings
from orchestrator.core.db import session_scope
from orchestrator.core.models import Notification, Run, Task
from orchestrator.core.notification_vocab import (
    MASTER_SETTING,
    NotificationTrigger,
    trigger_for_event,
)
from orchestrator.services import artifact_service, settings_service
from orchestrator.utils.redaction import redact

log = logging.getLogger(__name__)
COALESCE_SECONDS = 2.0  # one short global window: parallel finishes become one popup
DELIVERY_TIMEOUT_SECONDS = 10
PENDING, DELIVERED, FAILED, SKIPPED, COALESCED = (
    "pending", "delivered", "failed", "skipped", "coalesced",
)
_timer: threading.Timer | None = None
_lock = threading.Lock()
# Serialises delivery so two flushes (a timer thread and an inline sync-path flush) can never
# select the same pending row and deliver it twice.
_deliver_lock = threading.Lock()


def _human(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    s = float(ms) / 1000
    return f"{s:.0f}s" if s < 90 else (f"{s / 60:.1f}m" if s < 6000 else f"{s / 60:.0f}m")


def duration_details(run: Run | None) -> dict[str, Any]:
    """Both windows: run.json.process_ms (what the deadline used) and DB wall time."""
    if run is None or run.id is None:
        return {}
    archived = artifact_service.read_json(run.id, "run.json") or {}
    try:
        wall = int(run.duration_ms) if run.duration_ms is not None else None
        compute = int(archived["process_ms"]) if archived.get("process_ms") is not None else None
    except (TypeError, ValueError):
        return {}
    out: dict[str, Any] = {"compute_ms": compute, "wall_ms": wall}
    if compute is None or not wall:
        return out
    text = f"{_human(compute)} compute / {_human(wall)} wall"
    gap = wall - compute
    if gap > 0.25 * wall:
        out["suspended_ms"] = gap
        text += f"; clock suspended {_human(gap)}"
    out["text"] = text
    return out


def _task_link(task: Task | None) -> str:
    if task is None or task.id is None:
        return ""
    s = get_settings()
    host = s.bind_host if s.bind_host not in ("0.0.0.0", "::") else "127.0.0.1"  # noqa: S104
    return f"http://{host}:{s.bind_port}/tasks/{task.id}"


def _outcome(trigger: NotificationTrigger, run: Run | None, details: str) -> str:
    code = run.exit_code if run else "unknown"
    error = f": {run.error}" if run and run.error else ""
    key = trigger.key
    if key == "run_completed":
        return f"run finished normally (exit {code})"
    if key == "run_failed":
        return f"run failed (exit {code}){error}"
    if key == "run_timeout":
        return f"run hit the adapter timeout{error}"
    if key == "run_cancelled":
        return f"run cancelled (exit {code})"
    if key == "task_review":
        return "task is in review and waiting for a human decision"
    if key == "task_blocked":
        reason = details.partition(":")[2].strip()
        return "task is blocked" + (f": {reason}" if reason else "")
    return {
        "escalated": "task was escalated to an Architect review",
        "merge_candidate": "accepted; merge gate off, waiting for a manual merge",
        "merged": "accepted phase branch merged with --no-ff",
        "merge_refused": "the merge gate refused this branch",
    }.get(key, trigger.label.lower())


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
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
    )
    return {n: os.environ[n] for n in names if n in os.environ}


def _run_client(argv: list[str]) -> tuple[str, str | None]:
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            argv, env=_notification_env(), capture_output=True, text=True,
            timeout=DELIVERY_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return argv[0], f"timed out after {DELIVERY_TIMEOUT_SECONDS}s"
    except OSError as e:
        return argv[0], redact(str(e))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-500:]
        return argv[0], redact(err or f"exit {proc.returncode}")
    return argv[0], None


def _markup(body: str, link: str) -> str:
    """Escape user text and turn the task link into one real anchor (body-markup daemons)."""
    lines = [
        html.escape(x, quote=False) for x in body.splitlines() if x != f"Open: {link}"
    ]
    if link:
        lines.append(f'<a href="{html.escape(link, quote=True)}">Open task</a>')
    return "\n".join(lines)


def deliver(title: str, body: str, link: str = "") -> tuple[str | None, str | None]:
    """Send one desktop notification. Returns (channel, error)."""
    markup = _markup(body, link)
    if shutil.which("notify-send"):
        return _run_client([
            "notify-send", "--app-name=Coding Orchestrator", "--urgency=normal",
            "--icon=dialog-information", "--action=default=Open task", title, markup,
        ])
    if shutil.which("gdbus"):
        hints = "{'body-markup': <true>}" if link else "{}"
        return _run_client([
            "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications", "--method",
            "org.freedesktop.Notifications.Notify", json.dumps("Coding Orchestrator"), "0",
            json.dumps(""), json.dumps(title), json.dumps(markup),
            "['default', 'Open task']", hints, "10000",
        ])
    return None, "no freedesktop notification client on PATH (notify-send/gdbus)"


def _deliver_pending_locked(session: Session) -> int:
    """Deliver every pending row as one notification, recording each row's outcome."""
    rows = list(session.exec(
        select(Notification).where(Notification.status == PENDING).order_by(col(Notification.id))
    ))
    if not rows:
        return 0
    first = rows[0]
    title, body = first.title, first.body
    if len(rows) > 1:
        title = redact(f"Coding Orchestrator — {len(rows)} updates")
        body = redact("\n\n---\n\n".join(f"[{r.trigger}] {r.title}\n{r.body}" for r in rows))
    try:
        channel, error = deliver(title, body, first.link)
    except Exception as e:  # a notifier bug is recorded, never propagated
        channel, error = None, redact(str(e))[:1000]
    first.title, first.body, first.channel, first.error = title, body, channel, error
    first.status = DELIVERED if channel and error is None else FAILED
    if first.status == FAILED:
        first.reason = "delivery_failed"
    for extra in rows[1:]:
        extra.status, extra.coalesced_into = COALESCED, first.id
        extra.channel, extra.reason = channel, f"coalesced_into:{first.id}"
    for row in rows:
        session.add(row)
    session.commit()
    return first.id or 0


def _deliver_pending(session: Session) -> int:
    """One delivery at a time: a second flush re-selects only what is still pending."""
    with _deliver_lock:
        return _deliver_pending_locked(session)


def _flush_thread() -> None:
    global _timer
    with _lock:
        _timer = None  # before delivery, so an event arriving mid-flush schedules again
    try:
        flush_pending()
    except Exception:  # noqa: BLE001 - the timer thread must never raise
        log.warning("notification flush failed", exc_info=True)


def _schedule() -> None:
    global _timer
    with _lock:
        if _timer is not None:
            return
        _timer = threading.Timer(COALESCE_SECONDS, _flush_thread)
        _timer.daemon = True
        _timer.start()


def flush_pending(session: Session | None = None) -> int:
    """Deliver all pending rows now (timer, API startup recovery, tests).

    Never raises and never leaves a caller-supplied session poisoned: startup recovery runs
    inside the API lifespan, where an exception would abort the whole server.
    """
    try:
        with (nullcontext(session) if session is not None else session_scope()) as s:
            return _deliver_pending(s)
    except Exception as e:  # noqa: BLE001 - delivery is best-effort by contract
        try:
            if session is not None:
                session.rollback()
        except Exception:  # noqa: BLE001, S110 - rollback of an already-dead session
            pass
        log.warning("notification flush failed: %s", redact(str(e)))
        return 0


def record_failure(
    event: str, task_id: int | None, run_id: int | None, error: Exception
) -> None:
    """A fan-out bug still leaves one durable, deduped failed row; never raises.

    Public because `log_service.log_event` also calls it when the fan-out raises outside
    `notify_event`: the dedupe key makes the failure recorded at most once either way.
    """
    try:
        with session_scope() as s:
            key = f"fanout-error:{event}:{run_id}:{task_id}"
            if s.exec(select(Notification).where(Notification.dedupe_key == key)).first():
                return
            s.add(Notification(
                task_id=task_id, run_id=run_id, trigger="unknown", event=event,
                status=FAILED, reason="fanout_error", title="Notification fan-out failed",
                body=redact(f"{event}: {error}")[:1000], dedupe_key=key,
                error=redact(str(error))[:1000], details_json={},
            ))
            s.commit()
    except Exception:  # noqa: BLE001 - recording a failure must not become a second failure
        log.warning("could not record notification failure", exc_info=True)


def notify_event(
    *, event: str, details: str = "", task_id: int | None = None, run_id: int | None = None
) -> Notification | None:
    """Fan one event out to the desktop and the persistent list, in its own session."""
    try:
        trigger = trigger_for_event(event, details)
        if trigger is None:
            return None
        with session_scope() as session:
            task = session.get(Task, task_id) if task_id else None
            if run_id:
                run = session.get(Run, run_id)
            elif task_id:
                run = session.exec(
                    select(Run).where(Run.task_id == task_id).order_by(col(Run.id).desc())
                ).first()
            else:
                run = None
            if task is None and run is not None:
                task = session.get(Task, run.task_id)
            tid = task.id if task else task_id
            rid = run.id if run else run_id
            dedupe_key = f"{rid if rid is not None else f'task:{tid}'}:{trigger.key}"
            existing = session.exec(
                select(Notification).where(Notification.dedupe_key == dedupe_key)
            ).first()
            if existing is not None:
                return existing
            master = bool(settings_service.get(session, MASTER_SETTING))
            subscribed = bool(settings_service.get(session, trigger.setting))
            payload = _payload(trigger, task, run, details)
            enabled = master and subscribed
            row = Notification(
                task_id=tid, run_id=rid, trigger=trigger.key, event=event,
                title=payload["title"], body=payload["body"], link=payload["link"],
                dedupe_key=dedupe_key, details_json=payload["details"],
                status=PENDING if enabled else SKIPPED,
                reason="" if enabled else (
                    "master_disabled" if not master else "trigger_disabled"
                ),
            )
            session.add(row)
            session.commit()
            if row.status == PENDING:
                if COALESCE_SECONDS <= 0:
                    _deliver_pending(session)
                else:
                    _schedule()
            return row
    except Exception as e:  # noqa: BLE001 - notification is best-effort by contract
        record_failure(event, task_id, run_id, e)
        return None
