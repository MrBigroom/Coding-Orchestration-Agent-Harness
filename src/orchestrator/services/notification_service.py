"""Fire-and-forget desktop notifications for events that need a human (T-9)."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.core.config import get_settings
from orchestrator.core.db import session_scope
from orchestrator.core.models import Agent, Notification, Run, StandaloneRun, Task
from orchestrator.core.notification_vocab import (
    MASTER_SETTING,
    NotificationTrigger,
    trigger_for_event,
)
from orchestrator.services import artifact_service, settings_service
from orchestrator.utils.redaction import redact

log = logging.getLogger(__name__)
COALESCE_SECONDS = 2.0
COALESCE_MAX_SECONDS = 10.0
DELIVERY_TIMEOUT_SECONDS = 10
PROBE_TIMEOUT_SECONDS = 2
PENDING, DELIVERED, FAILED, SKIPPED, COALESCED = (
    "pending", "delivered", "failed", "skipped", "coalesced",
)
# Every transport result lands in exactly one outcome class, mapped to a frozen CCR-T9-01
# status. `shown_unacknowledged` (displayed, nothing acknowledged it) has no status of its own,
# so it is `delivered` with `reason=shown_unacknowledged`; a sixth status needs an approved CCR.
OUTCOME_DELIVERED = "delivered"
OUTCOME_SHOWN_UNACKNOWLEDGED = "shown_unacknowledged"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_COALESCED = "coalesced"
OUTCOME_CLASSES = (
    OUTCOME_DELIVERED, OUTCOME_SHOWN_UNACKNOWLEDGED, OUTCOME_FAILED, OUTCOME_SKIPPED,
    OUTCOME_COALESCED,
)
STATUS_FOR_OUTCOME = {
    OUTCOME_DELIVERED: DELIVERED,
    OUTCOME_SHOWN_UNACKNOWLEDGED: DELIVERED,
    OUTCOME_FAILED: FAILED,
    OUTCOME_SKIPPED: SKIPPED,
    OUTCOME_COALESCED: COALESCED,
}
PROCESS_EXIT_REASON = "process_exit_undelivered"
# The CLI exit terminates only rows its own process created, never a live server's rows.
OWNER_KEY = "owner"
PROCESS_TOKEN = f"{os.getpid()}:{uuid.uuid4().hex}"
MARKUP_CAPABILITY = "body-markup"
HYPERLINK_CAPABILITY = "body-hyperlinks"

_timer: threading.Timer | None = None
_batch_started: float | None = None
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


def _base_url() -> str:
    s = get_settings()
    host = s.bind_host if s.bind_host not in ("0.0.0.0", "::") else "127.0.0.1"  # noqa: S104
    return f"http://{host}:{s.bind_port}"


def _task_link(task: Task | None) -> str:
    if task is None or task.id is None:
        return ""
    return f"{_base_url()}/tasks/{task.id}"


def _agent_link() -> str:
    """Standalone runs have no task; their report lives in the agent Outputs view."""
    return f"{_base_url()}/agents"


def _outcome(
    trigger: NotificationTrigger,
    run: Run | None,
    details: str,
    standalone: StandaloneRun | None = None,
) -> str:
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
    if key == "standalone_run":
        status = standalone.status if standalone else "unknown"
        standalone_error = f": {standalone.error}" if standalone and standalone.error else ""
        return f"standalone run {status}{standalone_error}"
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
    trigger: NotificationTrigger,
    task: Task | None,
    run: Run | None,
    details: str,
    *,
    standalone: StandaloneRun | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    outcome = _outcome(trigger, run, details, standalone)
    duration = duration_details(run) if trigger.terminal_run else {}
    if standalone is not None:
        label = agent_name or standalone.title or f"agent {standalone.agent_id}"
        report = standalone.report_path or "no report file saved"
        lines = [
            f"Agent: {label}",
            f"Report: {standalone.title or report}",
            f"Run: standalone #{standalone.id} ({standalone.status})",
            f"Outcome: {outcome}",
            f"Report file: {report}",
            f"Next: {trigger.action}",
        ]
        link = _agent_link()
    else:
        label = f"{task.short_id} {task.title}" if task else "orchestrator"
        lines = [
            f"Task: {label}",
            f"Run: {run.short_id if run else 'no run'} ({run.status if run else 'n/a'})",
            f"Outcome: {outcome}",
            f"Next: {trigger.action}",
        ]
        link = _task_link(task)
    if duration.get("text"):
        lines.append(f"Duration: {duration['text']}")
    if link:
        lines.append(f"Open: {link}")
    detail = " ".join((details or "").split())
    if detail:
        lines.append(f"Detail: {detail[:400]}")
    details_json: dict[str, Any] = {
        "trigger": trigger.key,
        "outcome": redact(outcome),
        "duration": duration,
        "detail": redact(detail[:400]),
    }
    if standalone is not None:
        details_json["standalone_run_id"] = standalone.id
        details_json["report_path"] = redact(standalone.report_path or "")
    return {
        "title": redact(f"{label} — {trigger.label}"),
        "body": redact("\n".join(lines)),
        "link": redact(link),
        "details": details_json,
    }


def _notification_env() -> dict[str, str]:
    """Selected session vars only: never pass provider keys to a notification client."""
    names = (
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
    )
    return {n: os.environ[n] for n in names if n in os.environ}


@dataclass(frozen=True)
class TransportResult:
    """One transport attempt and the shape/daemon that produced it."""

    outcome: str
    channel: str | None = None
    error: str | None = None
    shape: str = "plain"
    daemon: str = "unprobed"
    detail: str = ""

    @property
    def status(self) -> str:
        return STATUS_FOR_OUTCOME[self.outcome]

    @property
    def reason(self) -> str:
        return {
            OUTCOME_DELIVERED: "",
            OUTCOME_SHOWN_UNACKNOWLEDGED: OUTCOME_SHOWN_UNACKNOWLEDGED,
            OUTCOME_FAILED: "delivery_failed",
            OUTCOME_SKIPPED: "not_attempted",
            OUTCOME_COALESCED: "coalesced",
        }[self.outcome]

    def as_details(self) -> dict[str, Any]:
        return {
            "outcome_class": self.outcome,
            "body_shape": self.shape,
            "daemon": self.daemon,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DaemonCapabilities:
    """What the notification daemon says it supports (freedesktop GetCapabilities)."""

    probed: bool
    capabilities: frozenset[str]
    source: str

    def supports(self, name: str) -> bool:
        return self.probed and name in self.capabilities

    def label(self) -> str:
        if not self.probed:
            return f"unprobed ({self.source})"
        return f"{self.source}: " + ",".join(sorted(self.capabilities))


UNPROBED = DaemonCapabilities(False, frozenset(), "not probed")
_CAPABILITY_RE = re.compile(r"'([A-Za-z0-9-]+)'")
_NOTIFICATION_ID_RE = re.compile(r"uint32\s+(\d+)")


def probe_capabilities() -> DaemonCapabilities:
    """Ask the daemon what it can render; no gdbus/failure/timeout means unprobed (plain text)."""
    if not shutil.which("gdbus"):
        return DaemonCapabilities(False, frozenset(), "gdbus not on PATH")
    argv = [
        "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
        "--object-path", "/org/freedesktop/Notifications", "--method",
        "org.freedesktop.Notifications.GetCapabilities",
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            argv, env=_notification_env(), capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return DaemonCapabilities(False, frozenset(), "GetCapabilities timed out")
    except OSError as e:
        return DaemonCapabilities(False, frozenset(), f"GetCapabilities failed: {redact(str(e))}")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-200:]
        source = f"GetCapabilities exit {proc.returncode}"
        return DaemonCapabilities(False, frozenset(), redact(f"{source}: {err}" if err else source))
    return DaemonCapabilities(
        True, frozenset(_CAPABILITY_RE.findall(proc.stdout or "")), "GetCapabilities"
    )


def _run_client(argv: list[str]) -> tuple[str, str | None, str]:
    """Run one notification client. Returns (channel, error, stdout)."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell
            argv, env=_notification_env(), capture_output=True, text=True,
            timeout=DELIVERY_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            argv[0],
            f"timed out after {DELIVERY_TIMEOUT_SECONDS}s (client killed; display state unknown)",
            "",
        )
    except OSError as e:
        return argv[0], redact(str(e)), ""
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-500:]
        return argv[0], redact(err or f"exit {proc.returncode}"), proc.stdout or ""
    return argv[0], None, proc.stdout or ""


def _body_for(body: str, link: str, caps: DaemonCapabilities, channel: str) -> tuple[str, str]:
    """Escape text for a markup-capable daemon; add an anchor only on gdbus with hyperlinks.

    A `body-markup` daemon parses every body as markup and the spec has no hint for it, so both
    transports get escaped text and gdbus sends `{}`. Mako/dunst defaults and `notify-send` keep
    the `Open: <url>` line; an unprobed daemon gets raw text.
    """
    if not caps.supports(MARKUP_CAPABILITY):
        return body, "plain"
    lines = [html.escape(x, quote=False) for x in body.splitlines()]
    if channel == "gdbus" and link and caps.supports(HYPERLINK_CAPABILITY):
        lines = [x for x in lines if x != html.escape(f"Open: {link}", quote=False)]
        lines.append(f'<a href="{html.escape(link, quote=True)}">Open task</a>')
        return "\n".join(lines), "markup+hyperlink"
    return "\n".join(lines), "markup-escaped"


def deliver(title: str, body: str, link: str = "") -> TransportResult:
    """Send one notification and classify it; no `--action` (it implies a blocking `--wait`)."""
    caps = probe_capabilities()
    daemon = caps.label()
    if shutil.which("notify-send"):
        channel = "notify-send"
        text, shape = _body_for(body, link, caps, channel)
        channel, error, _ = _run_client([
            "notify-send", "--app-name=Coding Orchestrator", "--urgency=normal",
            "--icon=dialog-information", title, text,
        ])
        if error is not None:
            return TransportResult(OUTCOME_FAILED, channel, error, shape, daemon)
        return TransportResult(
            OUTCOME_SHOWN_UNACKNOWLEDGED, channel, None, shape, daemon,
            "notify-send returned 0; dismissal is not observed",
        )
    if shutil.which("gdbus"):
        channel = "gdbus"
        text, shape = _body_for(body, link, caps, channel)
        # No hints and no action array: body-markup is a capability, not a hint.
        channel, error, stdout = _run_client([
            "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications", "--method",
            "org.freedesktop.Notifications.Notify", json.dumps("Coding Orchestrator"), "0",
            json.dumps(""), json.dumps(title), json.dumps(text), "[]", "{}", "10000",
        ])
        if error is not None:
            return TransportResult(OUTCOME_FAILED, channel, error, shape, daemon)
        match = _NOTIFICATION_ID_RE.search(stdout)
        if match:
            return TransportResult(
                OUTCOME_DELIVERED, channel, None, shape, daemon,
                f"daemon accepted notification id {match.group(1)}",
            )
        return TransportResult(
            OUTCOME_SHOWN_UNACKNOWLEDGED, channel, None, shape, daemon,
            "Notify returned without a notification id",
        )
    return TransportResult(
        OUTCOME_FAILED, None,
        "no freedesktop notification client on PATH (notify-send/gdbus)", "plain", daemon,
    )


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
        result = deliver(title, body, first.link)
    except Exception as e:  # a notifier bug is recorded, never propagated
        result = TransportResult(OUTCOME_FAILED, None, redact(str(e))[:1000])
    first.title, first.body, first.channel, first.error = title, body, result.channel, result.error
    first.status, first.reason = result.status, result.reason
    first.details_json = {**(first.details_json or {}), **result.as_details()}
    for extra in rows[1:]:
        extra.status, extra.coalesced_into = STATUS_FOR_OUTCOME[OUTCOME_COALESCED], first.id
        extra.channel, extra.reason = result.channel, f"coalesced_into:{first.id}"
        extra.details_json = {
            **(extra.details_json or {}),
            "outcome_class": OUTCOME_COALESCED,
            "coalesced_into": first.id,
        }
    for row in rows:
        session.add(row)
    session.commit()
    return first.id or 0


def _deliver_pending(session: Session) -> int:
    """One delivery at a time: a second flush re-selects only what is still pending."""
    with _deliver_lock:
        return _deliver_pending_locked(session)


def _flush_thread() -> None:
    global _batch_started, _timer
    with _lock:
        _timer = None  # before delivery, so an event arriving mid-flush schedules again
        _batch_started = None
    try:
        flush_pending()
    except Exception:  # noqa: BLE001 - the timer thread must never raise
        log.warning("notification flush failed", exc_info=True)


def _schedule() -> None:
    """Debounce one message per batch, capped after the first row so a stream cannot starve it."""
    global _batch_started, _timer
    with _lock:
        now = time.monotonic()
        if _batch_started is None:
            _batch_started = now
        cap_left = COALESCE_MAX_SECONDS - (now - _batch_started)
        if _timer is not None:
            if cap_left <= 0:
                return  # the armed timer is already at the cap: let it deliver this batch
            _timer.cancel()
        delay = max(0.0, min(COALESCE_SECONDS, cap_left))
        _timer = threading.Timer(delay, _flush_thread)
        _timer.daemon = True
        _timer.start()


def flush_pending(session: Session | None = None) -> int:
    """Deliver pending rows now; never raises and never poisons a caller-supplied session."""
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


def flush_pending_if_any(session: Session) -> int:
    """Read-path flush (T-18 DoD 3): the durable list must not show rows nothing ever tried."""
    any_pending = session.exec(
        select(Notification.id).where(Notification.status == PENDING).limit(1)
    ).first()
    return flush_pending(session) if any_pending is not None else 0


def _terminate_pending_locked(
    session: Session, reason: str, error: str, owner: str | None = PROCESS_TOKEN
) -> int:
    """Terminate only this process's pending rows; another owner's row keeps `pending`."""
    rows = list(session.exec(
        select(Notification).where(Notification.status == PENDING).order_by(col(Notification.id))
    ))
    if owner is not None:
        rows = [r for r in rows if (r.details_json or {}).get(OWNER_KEY) == owner]
    for row in rows:
        row.status = STATUS_FOR_OUTCOME[OUTCOME_FAILED]
        row.reason = reason
        row.error = row.error or error
        row.details_json = {
            **(row.details_json or {}), "outcome_class": OUTCOME_FAILED, "detail": error,
        }
        session.add(row)
    if rows:
        session.commit()
    return len(rows)


def _database_ready() -> bool:
    """True only when this home already holds the notifications table (never create one on exit)."""
    from sqlalchemy import inspect

    from orchestrator.core import db

    url = get_settings().db_url or ""
    if url.startswith("sqlite:///") and not Path(url.removeprefix("sqlite:///")).exists():
        return False
    try:
        return bool(inspect(db.get_engine()).has_table(Notification.__tablename__))
    except Exception:  # noqa: BLE001 - an unreadable database is "not ready", not an exit error
        return False


def flush_pending_at_exit(session: Session | None = None) -> int:
    """CLI exit: deliver, then terminate what this process could not deliver. Never raises."""
    if session is None and not _database_ready():
        return 0
    delivered = flush_pending(session)
    try:
        with (nullcontext(session) if session is not None else session_scope()) as s:
            return delivered + _terminate_pending_locked(
                s, PROCESS_EXIT_REASON, "process exited before delivery was attempted"
            )
    except Exception as e:  # noqa: BLE001 - exiting must not fail because of a notification
        try:
            if session is not None:
                session.rollback()
        except Exception:  # noqa: BLE001, S110 - rollback of an already-dead session
            pass
        log.warning("notification exit flush failed: %s", redact(str(e)))
        return delivered


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
    *,
    event: str,
    details: str = "",
    task_id: int | None = None,
    run_id: int | None = None,
    standalone_run_id: int | None = None,
) -> Notification | None:
    """Fan one event out to the desktop and the persistent list, in its own session.

    A standalone run is a different table from `Run`, so its id never goes into
    `Notification.run_id` (the Notifications page would deep-link it as a task); it is used
    only for the payload and the dedupe identity.
    """
    try:
        trigger = trigger_for_event(event, details)
        if trigger is None:
            return None
        with session_scope() as session:
            standalone = (
                session.get(StandaloneRun, standalone_run_id) if standalone_run_id else None
            )
            if standalone is not None:
                task = None
                run = None
                tid = None
                rid = None
                agent = session.get(Agent, standalone.agent_id)
                agent_name = agent.name if agent else None
                dedupe_key = f"standalone:{standalone.id}:{trigger.key}"
            else:
                agent_name = None
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
                if rid is not None:
                    dedupe_key = f"{rid}:{trigger.key}"
                elif tid is not None:
                    dedupe_key = f"task:{tid}:{trigger.key}"
                else:
                    # Nothing identifies the event's owner (e.g. schedule_fired): a constant
                    # key would let the first such event silence every later one forever.
                    dedupe_key = f"event:{event}:{uuid.uuid4().hex}:{trigger.key}"
            existing = session.exec(
                select(Notification).where(Notification.dedupe_key == dedupe_key)
            ).first()
            if existing is not None:
                return existing
            master = bool(settings_service.get(session, MASTER_SETTING))
            subscribed = bool(settings_service.get(session, trigger.setting))
            payload = _payload(
                trigger, task, run, details, standalone=standalone, agent_name=agent_name
            )
            enabled = master and subscribed
            row = Notification(
                task_id=tid, run_id=rid, trigger=trigger.key, event=event,
                title=payload["title"], body=payload["body"], link=payload["link"],
                dedupe_key=dedupe_key,
                details_json={**payload["details"], OWNER_KEY: PROCESS_TOKEN},
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
