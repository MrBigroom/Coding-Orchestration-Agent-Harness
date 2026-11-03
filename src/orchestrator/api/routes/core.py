"""Health, dashboard, projects, settings, build log, prompt preview."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, select

from orchestrator import __version__
from orchestrator.api.dependencies import AUTH, SessionDep, load_project
from orchestrator.core import db
from orchestrator.core.models import (
    HarnessAdapter,
    Project,
    Provider,
    Run,
    TaskStatus,
)
from orchestrator.core.schemas import (
    ProjectCreate,
    ProjectUpdate,
    PromptPreviewRequest,
    SettingsUpdate,
)
from orchestrator.core.state import ACTIVE_RUN_STATES
from orchestrator.services import (
    adapter_service,
    chat_service,
    log_service,
    memory_service,
    process_service,
    project_service,
    prompt_builder,
    provider_service,
    settings_service,
    task_service,
    usage_service,
)

router = APIRouter()


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


@router.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness probe (reveals no data)."""
    try:
        rev = db.current_revision()
    except Exception:  # noqa: BLE001
        rev = None
    return {
        "ok": True,
        "version": __version__,
        "db_revision": rev,
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/dashboard", dependencies=AUTH)
def dashboard(session: Session = SessionDep) -> dict[str, Any]:
    runs = session.exec(select(Run).where(col(Run.status).in_(ACTIVE_RUN_STATES))).all()
    tasks = task_service.list_tasks(session)
    failed = []
    for t in tasks:
        lr = task_service.latest_run(session, t)
        if lr and lr.status in ("failed", "timeout") and t.status == TaskStatus.planned:
            failed.append(t)
    providers = [provider_service.provider_status(p) for p in session.exec(select(Provider))]
    adapters = [adapter_service.describe(a) for a in session.exec(select(HarnessAdapter))]
    return {
        "active_runs": [dump(r) for r in runs],
        "queued": [dump(t) for t in tasks if t.status == TaskStatus.queued],
        "live_chats": len(chat_service.manager.live),
        "blocked": [dump(t) for t in tasks if t.status == TaskStatus.blocked],
        "review": [dump(t) for t in tasks if t.status == TaskStatus.review],
        "failed": [dump(t) for t in failed],
        "counts": {s.value: sum(1 for t in tasks if t.status == s) for s in TaskStatus},
        "providers": providers,
        "adapters": adapters,
        "memory_pending": memory_service.pending_count(session),
        "recent_activity": [dump(e) for e in log_service.recent(session, 25)],
    }


# ---------------------------------------------------------------- projects


@router.get("/projects", dependencies=AUTH)
def list_projects(session: Session = SessionDep) -> list[dict[str, Any]]:
    return [dump(p) for p in session.exec(select(Project))]


@router.post("/projects", status_code=201, dependencies=AUTH)
def create_project(body: ProjectCreate, session: Session = SessionDep) -> dict[str, Any]:
    return dump(project_service.create(session, body))


@router.get("/projects/{project_id}", dependencies=AUTH)
def get_project(project: Project = Depends(load_project)) -> dict[str, Any]:
    return dump(project)


@router.patch("/projects/{project_id}", dependencies=AUTH)
def update_project(
    body: ProjectUpdate, project: Project = Depends(load_project), session: Session = SessionDep
) -> dict[str, Any]:
    return dump(project_service.update(session, project, body))


@router.get("/projects/{project_id}/doctor", dependencies=AUTH)
def project_doctor(project: Project = Depends(load_project)) -> dict[str, Any]:
    checks = project_service.doctor(project)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


@router.post("/projects/{project_id}/commands/{label}", dependencies=AUTH)
async def run_project_command(
    label: str, project: Project = Depends(load_project), session: Session = SessionDep
) -> dict[str, Any]:
    """Run the project's configured test/lint/build command in its main checkout.
    Disabled unless the `allow_gui_commands` setting is on. No arbitrary commands."""
    if not settings_service.get(session, "allow_gui_commands"):
        raise HTTPException(403, "GUI command execution is disabled (allow_gui_commands=false)")
    cmd = {
        "test": project.test_command,
        "lint": project.lint_command,
        "build": project.build_command,
    }.get(label)
    if label not in ("test", "lint", "build"):
        raise HTTPException(404, "unknown command label")
    if not cmd:
        raise HTTPException(400, f"no {label} command configured")
    if not project_service.command_allowed(project, cmd):
        raise HTTPException(403, "command not allowed by project settings")
    from pathlib import Path

    code, out, timed_out = await process_service.run_simple(
        project_service.command_argv(cmd), Path(project.path), 900
    )
    return {"command": cmd, "exit_code": code, "timed_out": timed_out, "output": out[-20000:]}


# ---------------------------------------------------------------- settings & logs


@router.get("/settings", dependencies=AUTH)
def get_settings_(session: Session = SessionDep) -> dict[str, Any]:
    return settings_service.get_all(session)


@router.patch("/settings", dependencies=AUTH)
def update_settings(body: SettingsUpdate, session: Session = SessionDep) -> dict[str, Any]:
    return settings_service.update(session, body.values)


@router.get("/build-log", dependencies=AUTH)
def build_log(
    limit: int = Query(200, ge=1, le=5000),
    task_id: int | None = None,
    source: str = Query("db", pattern="^(db|jsonl)$"),
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    if source == "jsonl":
        rows = log_service.tail_jsonl(limit)
        return [r for r in rows if task_id is None or r.get("task_id") == task_id]
    return [dump(e) for e in log_service.recent(session, limit, task_id)]


# ---------------------------------------------------------------- prompt preview


def _preview(session: Session, body: PromptPreviewRequest) -> dict[str, Any]:
    if body.sections is not None:
        return prompt_builder.preview(prompt_builder.sections_from_dicts(body.sections))
    task = task_service.get(session, body.task_id) if body.task_id else None
    project = None
    if task and task.project_id:
        project = session.get(Project, task.project_id)
    elif body.project_slug:
        project = project_service.get(session, body.project_slug)
    built = prompt_builder.build_prompt(session, body.role, task, project)
    return prompt_builder.preview(built.sections)


@router.post("/prompt-preview", dependencies=AUTH)
def prompt_preview(body: PromptPreviewRequest, session: Session = SessionDep) -> dict[str, Any]:
    return _preview(session, body)


@router.get("/cache-stats", dependencies=AUTH)
def cache_stats(session: Session = SessionDep) -> dict[str, Any]:
    return {"rows": usage_service.cache_stats(session)}


@router.get("/usage/daily", dependencies=AUTH)
def usage_daily(
    days: int = Query(30, ge=1, le=366),
    source: str | None = Query(None, pattern="^(task|chat|standalone|api)$"),
    session: Session = SessionDep,
) -> dict[str, Any]:
    return usage_service.daily(session, days, source)


@router.get("/prompt-preview", dependencies=AUTH)
def prompt_preview_get(
    role: str = "worker",
    task_id: int | None = None,
    project_slug: str | None = None,
    session: Session = SessionDep,
) -> dict[str, Any]:
    return _preview(
        session, PromptPreviewRequest(role=role, task_id=task_id, project_slug=project_slug)
    )
