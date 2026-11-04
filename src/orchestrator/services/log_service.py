"""Build log: append-only JSONL at ORCHESTRATOR_HOME/build-log.jsonl, mirrored to SQLite."""

from __future__ import annotations

import json
import logging
from collections import deque

from sqlmodel import Session, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import BuildLogEntry, utcnow
from orchestrator.core.schemas import BuildLogRecord
from orchestrator.utils.redaction import redact

log = logging.getLogger(__name__)


def log_event(
    session: Session,
    event: str,
    details: str = "",
    *,
    role: str = "system",
    phase: str | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    commit_sha: str | None = None,
) -> BuildLogEntry:
    now = utcnow()
    details = redact(details)
    if phase is None and task_id is not None:
        from orchestrator.core.models import Task

        task = session.get(Task, task_id)
        if task:
            phase = f"phase-{task.phase_number}" if task.phase_number is not None else task.short_id
    entry = BuildLogEntry(
        timestamp=now,
        phase=phase,
        task_id=task_id,
        run_id=run_id,
        role=role,
        event=event,
        details=details,
        commit_sha=commit_sha,
    )
    session.add(entry)
    session.commit()
    record = BuildLogRecord(
        timestamp=now.isoformat(),
        phase=phase,
        task_id=task_id,
        run_id=run_id,
        role=role,
        event=event,
        details=details,
        commit_sha=commit_sha,
    )
    path = get_settings().build_log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.model_dump_json() + "\n")
    # T-9: the single chokepoint every emitter already goes through. Called AFTER the row is
    # committed and the JSONL line is written, and wrapped so a notification problem (missing
    # D-Bus, raising notifier, no desktop) can never lose the build-log line or touch a status.
    try:
        from orchestrator.services import notification_service

        notification_service.notify_event(
            event=event, details=details, task_id=task_id, run_id=run_id
        )
    except Exception as e:  # noqa: BLE001 - notification is best-effort by contract
        # The fan-out uses its own session, but a failure here must still leave the caller's
        # session usable: roll back only if SQLAlchemy already marked it failed.
        try:
            if not session.is_active:
                session.rollback()
        except Exception:  # noqa: BLE001, S110
            pass
        log.warning("notification fan-out failed for %s: %s", event, redact(str(e)))
    return entry


def tail_jsonl(limit: int = 200) -> list[dict]:
    path = get_settings().build_log_path
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        lines = deque(fh, maxlen=limit)
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def recent(session: Session, limit: int = 100, task_id: int | None = None) -> list[BuildLogEntry]:
    q = select(BuildLogEntry).order_by(BuildLogEntry.id.desc()).limit(limit)  # type: ignore[union-attr]
    if task_id is not None:
        q = q.where(BuildLogEntry.task_id == task_id)
    return list(session.exec(q))
