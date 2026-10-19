"""Runtime-editable settings: env defaults overridden by rows in the `settings` table."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from orchestrator.core.config import get_settings
from orchestrator.core.models import Setting, utcnow

EDITABLE: dict[str, type] = {
    "merge_enabled": bool,
    "auto_merge_enabled": bool,
    "supervisor_microfixes_enabled": bool,
    "max_parallel_runs": int,
    "max_parallel_runs_per_project": int,
    "scheduler_enabled": bool,
    "worker_enabled": bool,
    "allow_gui_commands": bool,
    "max_review_rounds": int,
    "theme": str,
}


def defaults() -> dict[str, Any]:
    s = get_settings()
    return {
        "merge_enabled": s.merge_enabled,
        "auto_merge_enabled": s.auto_merge_enabled,
        "supervisor_microfixes_enabled": s.supervisor_microfixes_enabled,
        "max_parallel_runs": s.max_parallel_runs,
        "max_parallel_runs_per_project": s.max_parallel_runs_per_project,
        "scheduler_enabled": s.enable_scheduler,
        "worker_enabled": s.enable_worker,
        "allow_gui_commands": s.allow_gui_commands,
        "max_review_rounds": 3,
        "theme": "dark",
    }


def get_all(session: Session) -> dict[str, Any]:
    values = defaults()
    for key in EDITABLE:
        row = session.get(Setting, key)
        if row is not None:
            values[key] = row.value_json
    s = get_settings()
    values["token_auth_enabled"] = bool(s.token)
    values["bind_host"] = s.bind_host
    values["bind_port"] = s.bind_port
    return values


def get(session: Session, key: str) -> Any:
    return get_all(session)[key]


def update(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    for key, val in values.items():
        if key not in EDITABLE:
            raise KeyError(f"setting {key!r} is not editable")
        typ = EDITABLE[key]
        if typ is bool and not isinstance(val, bool):
            raise ValueError(f"{key} must be a boolean")
        if typ is int and (not isinstance(val, int) or isinstance(val, bool) or val < 1):
            raise ValueError(f"{key} must be a positive integer")
        if typ is str and not isinstance(val, str):
            raise ValueError(f"{key} must be a string")
        row = session.get(Setting, key) or Setting(key=key)
        row.value_json = val
        row.updated_at = utcnow()
        session.add(row)
    session.commit()
    return get_all(session)
