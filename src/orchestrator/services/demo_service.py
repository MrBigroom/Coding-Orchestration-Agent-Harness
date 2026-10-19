"""`orchestrator demo`: the full role loop offline, with mock provider + mock harness."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import MemoryEntry, Project, TaskKind
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
    bootstrap,
    memory_service,
    project_service,
    run_service,
    task_service,
)
from orchestrator.utils.git import is_repo, run_git

SAMPLE_NAME = "sample-project"


def sample_source() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "examples" / SAMPLE_NAME
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("examples/sample-project not found next to the installation")


def ensure_sample_repo() -> Path:
    dest = get_settings().home / SAMPLE_NAME
    if is_repo(dest):
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(sample_source(), dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    s = get_settings()
    run_git(["init", "-q", "-b", "main"], dest)
    run_git(["add", "-A"], dest)
    run_git(["-c", f"user.name={s.git_user_name}", "-c", f"user.email={s.git_user_email}",
             "commit", "-q", "-m", "sample project: initial commit"], dest)
    return dest


def ensure_sample_project(session: Session) -> Project:
    existing = session.exec(select(Project).where(Project.name == SAMPLE_NAME)).first()
    if existing:
        return existing
    path = ensure_sample_repo()
    return project_service.create(session, ProjectCreate(
        name=SAMPLE_NAME, path=str(path), base_branch="main",
        test_command="python3 run_tests.py",
        notes="Demo project created by `orchestrator demo`."))


async def run_demo(session: Session, extra_tags: list[str] | None = None) -> dict[str, Any]:
    steps: list[str] = []
    info = bootstrap.init()
    steps.append(f"initialised orchestrator home at {info['home']}")
    project = ensure_sample_project(session)
    steps.append(f"project '{project.name}' registered at {project.path}")

    n = len(task_service.list_tasks(session, project_id=project.id, include_archived=True)) + 1
    task = task_service.create(session, TaskCreate(
        title=f"Demo phase {n}: append a mock change",
        project_id=project.id, kind=TaskKind.coding, tags=["mock", *(extra_tags or [])],
        phase_number=n, phase_slug=f"demo-{n}",
        description="Append one deterministic line to MOCK_CHANGE.md and keep tests green.",
        definition_of_done=["MOCK_CHANGE.md contains a line for this run",
                            "`python3 run_tests.py` passes"],
        acceptance_criteria=["handoff.json, review.json and verification.json exist"],
        frozen_contracts=["calc.add/calc.mul signatures"], out_of_scope=["new dependencies"],
    ))
    steps.append(f"created mock phase task {task.short_id} on branch {task.branch_name}")

    outcome = await run_service.run_task_now(session, task)
    session.refresh(task)
    run_id = outcome.get("run_id")
    steps.append(f"mock harness run R-{run_id}: {outcome.get('status')}")
    if run_id:
        names = [a["name"] for a in artifact_service.list_artifacts(run_id)]
        steps.append("artifacts: " + ", ".join(names))
    steps.append(f"supervisor verification: {outcome.get('verification')}")
    steps.append(f"architect review: {outcome.get('verdict')}")
    steps.append(f"final task state: {task.status} (merge: {task.merge_status})")
    if outcome.get("merge_command"):
        steps.append(f"merge gate disabled -> run manually: {outcome['merge_command']}")

    pending = [e for e in memory_service.list_entries(session, status="pending")
               if e.source.endswith(f"R-{run_id}")]
    approved: MemoryEntry | None = None
    if pending:
        approved = memory_service.approve(session, pending[0].id or 0)
        steps.append(f"{len(pending)} memory suggestion(s) from handoff; approved #{approved.id}"
                     f" ({approved.category}/{approved.scope})")
    return {"task": task.short_id, "task_id": task.id, "run_id": run_id,
            "status": task.status, "outcome": outcome, "steps": steps,
            "approved_memory_id": approved.id if approved else None}
