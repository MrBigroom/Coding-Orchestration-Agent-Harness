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
    "allow_interactive_sessions": bool,
    "allow_unattended_sessions": bool,
    "supabase_sync_enabled": bool,
    "supabase_sync_interval_seconds": int,
    "max_review_rounds": int,
    # T-12: a separate cap on every adapter attempt, not just the runs that reach the gate.
    # `max_review_rounds` still counts completed/verified rounds; this one also counts timeout,
    # failed and cancelled runs so a Worker that never submits cannot be retried forever.
    "max_run_attempts_per_task": int,
    "theme": str,
    # {"<model-name prefix or profile name>": ["<adapter>", ...]}; merged over the code
    # defaults in adapter_service.DEFAULT_HARNESS_ROUTES.
    "harness_routing": dict,
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
        "allow_interactive_sessions": s.allow_interactive_sessions,
        "allow_unattended_sessions": s.allow_unattended_sessions,
        "supabase_sync_enabled": s.supabase_sync_enabled,
        "supabase_sync_interval_seconds": s.supabase_sync_interval_seconds,
        "max_review_rounds": 3,
        "max_run_attempts_per_task": 6,
        "theme": "dark",
        "harness_routing": {},
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
    values["supabase_configured"] = all(
        (
            s.supabase_url,
            s.supabase_publishable_key,
            s.supabase_email,
            s.supabase_password,
        )
    )
    values["supabase_url"] = s.supabase_url
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
        if key == "supabase_sync_interval_seconds" and not 15 <= val <= 86400:
            raise ValueError(f"{key} must be between 15 and 86400 seconds")
        if typ is str and not isinstance(val, str):
            raise ValueError(f"{key} must be a string")
        if typ is dict and not (
            isinstance(val, dict)
            and all(
                isinstance(k, str) and isinstance(v, list) and all(isinstance(a, str) for a in v)
                for k, v in val.items()
            )
        ):
            raise ValueError(f"{key} must map strings to lists of adapter names")
        row = session.get(Setting, key) or Setting(key=key)
        row.value_json = val
        row.updated_at = utcnow()
        session.add(row)
    session.commit()
    return get_all(session)
