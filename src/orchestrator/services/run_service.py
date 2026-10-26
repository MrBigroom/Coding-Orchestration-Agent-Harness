"""One Worker run, end to end, as a state machine over artifacts:

  pending -> preparing (route, worktree, prompt) -> running (harness or direct API)
          -> testing (supervisor verification) -> completed | failed | timeout | cancelled

then hands over to the Supervisor gate (review/accept/requeue/block/merge).
"""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session

from orchestrator.adapters.base import AdapterResult, RunContext, TemplateError
from orchestrator.core.events import bus
from orchestrator.core.models import (
    Project,
    Review,
    Role,
    Run,
    RunStatus,
    Task,
    TaskKind,
    TaskStatus,
    utcnow,
)
from orchestrator.core.schemas import Handoff
from orchestrator.core.state import check_run_transition
from orchestrator.providers.base import ProviderError
from orchestrator.services import (
    adapter_service,
    artifact_service,
    git_service,
    log_service,
    memory_service,
    process_service,
    prompt_builder,
    provider_service,
    supervisor_service,
    task_service,
    usage_service,
)
from orchestrator.services.prompt_builder import DynamicState
from orchestrator.utils.git import head_sha
from orchestrator.utils.redaction import redact


@dataclass
class RoutePlan:
    harness_adapter: str | None
    harness_reason: str
    provider_profile: str
    model: str
    provider_reason: str
    command_preview: str | None
    worktree_path: str | None
    branch: str | None
    would_block: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _set_run(session: Session, run: Run, status: str, **fields: Any) -> Run:
    check_run_transition(run.status, status)
    run.status = RunStatus(status).value
    for k, v in fields.items():
        setattr(run, k, v)
    session.add(run)
    session.commit()
    bus.publish(run.id or 0, {"type": "status", "status": run.status})
    return run


def _project(session: Session, task: Task) -> Project | None:
    return session.get(Project, task.project_id) if task.project_id else None


def dry_run(session: Session, task: Task) -> RoutePlan:
    """What would run, without executing anything or touching git."""
    project = _project(session, task)
    role = Role.agent if task.kind == TaskKind.agent else Role.worker
    profile = provider_service.choose_profile(session, task, role)
    harness = adapter_service.choose_harness(
        session, task, profile.profile.name, profile.profile.model_name
    )
    preview = None
    wt = None
    if task.kind != TaskKind.agent and project:
        wt = str(git_service.worktree_path(project.slug, task.id or 0))
    if harness.adapter and task.kind != TaskKind.agent:
        adapter = adapter_service.get_adapter(session, harness.adapter)
        ctx = RunContext(
            run_id=0,
            task_id=task.id or 0,
            cwd=Path(wt or "."),
            prompt_file=Path("prompt.md"),
            artifacts_dir=Path("runs/<run_id>"),
            handoff_path=Path(wt or ".") / git_service.ORCH_DIR / "handoff.json",
            branch=task.branch_name or "",
            phase=task_service.phase_label(task),
            tags=task.tags,
            model=None,
        )
        preview = shlex.join(adapter.command_preview(ctx))
    would_block = (
        harness.adapter is None and task.kind != TaskKind.agent and "will block" in harness.reason
    )
    return RoutePlan(
        harness_adapter=None if task.kind == TaskKind.agent else harness.adapter,
        harness_reason="agent tasks call the model directly"
        if task.kind == TaskKind.agent
        else harness.reason,
        provider_profile=profile.profile.name,
        model=profile.profile.model_name,
        provider_reason=profile.reason,
        command_preview=preview,
        worktree_path=wt,
        branch=task.branch_name,
        would_block=would_block,
    )


def _latest_feedback(session: Session, task: Task) -> str | None:
    from sqlmodel import col, select

    review = session.exec(
        select(Review).where(Review.task_id == task.id).order_by(col(Review.id).desc())
    ).first()
    if not review or review.verdict == "APPROVED":
        return None
    items = "\n".join(
        f"- {f.get('file_or_area')}: {f.get('issue')} (violates {f.get('violates') or 'n/a'})"
        for f in review.findings
    )
    return f"Verdict: {review.verdict}\n{items}"


def _synthesize_handoff(
    task: Task, run: Run, files: list[str], branch: str, result: AdapterResult
) -> Handoff:
    return Handoff(
        phase=task_service.phase_label(task),
        branch=branch,
        status="READY_FOR_REVIEW",
        summary="No valid handoff.json was produced by the worker. This handoff was "
        "synthesized by the orchestrator from observed evidence only.",
        files_changed=files,
        assumptions=["synthesized by orchestrator; worker self-check unavailable"],
        blockers=[] if result.exit_code == 0 else [f"worker exit code {result.exit_code}"],
    )


async def execute(
    session: Session,
    task: Task,
    harness_override: str | None = None,
    profile_override: str | None = None,
) -> dict[str, Any]:
    """Run the Worker for a task that is already `in_progress` (claimed)."""
    if task.status != TaskStatus.in_progress:
        raise task_service.TaskError(
            f"task {task.short_id} must be in_progress (claimed); it is {task.status}"
        )
    project = _project(session, task)
    run = Run(
        short_id=f"tmp-{uuid.uuid4().hex}",
        task_id=task.id or 0,
        status=RunStatus.pending,
        branch_name=task.branch_name,
    )
    session.add(run)
    session.flush()
    run.short_id = f"R-{run.id}"
    assert run.id is not None
    run.artifacts_path = str(artifact_service.run_dir(run.id))
    session.add(run)
    session.commit()
    log_service.log_event(
        session,
        "run_created",
        run.short_id,
        role="supervisor",
        task_id=task.id,
        run_id=run.id,
        phase=task_service.phase_label(task),
    )

    try:
        return await _execute(session, task, run, project, harness_override, profile_override)
    except Exception as e:  # never leave a run dangling in a non-terminal state
        session.rollback()
        session.refresh(run)
        session.refresh(task)
        if run.status not in ("completed", "failed", "cancelled", "timeout"):
            _set_run(
                session, run, RunStatus.failed, error=redact(str(e))[:2000], finished_at=utcnow()
            )
        if task.status == TaskStatus.in_progress:
            task_service.transition(
                session, task, TaskStatus.planned, f"run failed: {e}", role="supervisor"
            )
        log_service.log_event(
            session, "run_error", str(e), role="supervisor", task_id=task.id, run_id=run.id
        )
        return {"run_id": run.id, "status": run.status, "error": str(e)}


async def _execute(
    session: Session,
    task: Task,
    run: Run,
    project: Project | None,
    harness_override: str | None,
    profile_override: str | None,
) -> dict[str, Any]:
    assert run.id is not None and task.id is not None
    started = utcnow()
    _set_run(session, run, RunStatus.preparing, started_at=started)

    if task.kind == TaskKind.agent or (task.kind != TaskKind.agent and project is None):
        return await _execute_direct(session, task, run, project, profile_override)

    assert project is not None
    if profile_override:
        task.assigned_provider_profile = profile_override
    profile = provider_service.choose_profile(session, task, Role.worker).profile
    harness_name = (
        harness_override
        or adapter_service.choose_harness(session, task, profile.name, profile.model_name).adapter
    )
    if harness_name is None:
        if task.risk == "low":
            return await _execute_direct(session, task, run, project, profile_override)
        _set_run(
            session,
            run,
            RunStatus.failed,
            error="no suitable harness available",
            finished_at=utcnow(),
        )
        task_service.block(session, task, "no harness available for a non-simple task")
        return {"run_id": run.id, "status": run.status, "decision": "blocked"}

    adapter = adapter_service.get_adapter(session, harness_name)
    if not adapter.spec.enabled:
        raise RuntimeError(f"adapter {harness_name} is disabled")
    if not adapter.detect():
        raise RuntimeError(f"adapter {harness_name}: `{adapter.spec.command}` not found on PATH")

    assert task.branch_name
    wt = git_service.ensure_worktree(
        Path(project.path), project.slug, task.id, task.branch_name, project.base_branch
    )
    if wt.created:
        log_service.log_event(
            session,
            "worktree_created",
            str(wt.path),
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
    # Supervisor duty: check git status BEFORE (re)trying; recover dirty trees safely.
    rec = git_service.recovery_commit(wt.path, f"leftover changes before run {run.short_id}")
    if rec:
        log_service.log_event(
            session,
            "recovery_commit",
            "dirty worktree before retry",
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
            commit_sha=rec,
        )
    handoff_path = wt.path / git_service.ORCH_DIR / "handoff.json"
    handoff_path.parent.mkdir(exist_ok=True)
    handoff_path.unlink(missing_ok=True)

    state = DynamicState(feedback=_latest_feedback(session, task))
    prompt = prompt_builder.build_prompt(session, Role.worker, task, project, state)
    prompt_text = prompt.static_text() + "\n\n" + prompt.dynamic_text()
    prompt_file = artifact_service.write_text(run.id, "prompt.md", prompt_text)

    ctx = RunContext(
        run_id=run.id,
        task_id=task.id,
        cwd=wt.path,
        prompt_file=prompt_file,
        artifacts_dir=artifact_service.run_dir(run.id),
        handoff_path=handoff_path,
        branch=task.branch_name,
        phase=task_service.phase_label(task),
        tags=task.tags,
        model=profile.model_name if harness_name != "mock" else None,
        timeout_seconds=adapter.spec.timeout_seconds,
    )
    try:
        preview = redact(shlex.join(adapter.command_preview(ctx)))
    except TemplateError as e:
        raise RuntimeError(str(e)) from e
    _set_run(
        session,
        run,
        RunStatus.running,
        harness_adapter=harness_name,
        provider_profile=profile.name,
        worktree_path=str(wt.path),
        branch_name=task.branch_name,
        command_preview=preview,
    )

    result = await adapter.run(ctx, process_service.run_process)
    finished = utcnow()
    duration = int((finished - started).total_seconds() * 1000)
    if result.stdout_path and result.stdout_path.is_file():
        usage = usage_service.harness_usage(
            adapter.spec.kind, result.stdout_path.read_text(errors="replace"), ctx.model
        )
        if usage:
            run.usage = usage  # persisted by the next _set_run commit
    artifact_service.write_json(
        run.id,
        "run.json",
        {
            "run": run.short_id,
            "task": task.short_id,
            "adapter": harness_name,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "process_ms": result.duration_ms,
        },
    )

    # Collect whatever landed, even on failure (evidence for the next retry).
    rec = git_service.recovery_commit(wt.path, f"uncommitted worker output from {run.short_id}")
    if rec:
        log_service.log_event(
            session,
            "recovery_commit",
            "worker left uncommitted changes",
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
            commit_sha=rec,
        )
    patch, stat = git_service.diff_against_base(wt.path, project.base_branch)
    artifact_service.write_text(run.id, "patch.diff", patch)
    artifact_service.write_text(run.id, "diffstat.txt", stat)
    sha = head_sha(wt.path)

    if result.cancelled:
        _set_run(
            session,
            run,
            RunStatus.cancelled,
            exit_code=result.exit_code,
            error="cancelled",
            finished_at=finished,
            duration_ms=duration,
            commit_sha=sha,
        )
        task_service.transition(
            session, task, TaskStatus.planned, "run cancelled", role="supervisor"
        )
        return {"run_id": run.id, "status": run.status}
    if result.timed_out or result.exit_code != 0:
        status = RunStatus.timeout if result.timed_out else RunStatus.failed
        _set_run(
            session,
            run,
            status,
            exit_code=result.exit_code,
            error=result.error or f"exit code {result.exit_code}",
            finished_at=finished,
            duration_ms=duration,
            commit_sha=sha,
        )
        task.handoff_status = "RUN_" + status.upper()
        task_service.transition(
            session, task, TaskStatus.planned, f"run {status}", role="supervisor"
        )
        log_service.log_event(
            session, f"run_{status}", run.error or "", role="worker", task_id=task.id, run_id=run.id
        )
        return {"run_id": run.id, "status": run.status, "error": run.error}

    handoff = adapter.parse_result(ctx, result)
    synthesized = handoff is None
    if handoff is None:
        handoff = _synthesize_handoff(
            task,
            run,
            git_service.changed_files(wt.path, project.base_branch),
            task.branch_name,
            result,
        )
    artifact_service.write_json(run.id, "handoff.json", handoff.model_dump(mode="json"))
    artifact_service.write_text(run.id, "handoff.md", artifact_service.render_handoff_md(handoff))
    if not synthesized and handoff.memory_suggestions:
        memory_service.suggest_from_handoff(
            session,
            handoff.memory_suggestions,
            source=f"handoff:{run.short_id}",
            project_slug=project.slug,
            role="worker",
        )
    task.handoff_status = handoff.status if not synthesized else "SYNTHESIZED"
    session.add(task)
    session.commit()

    if handoff.status == "BLOCKED":
        _set_run(
            session,
            run,
            RunStatus.completed,
            exit_code=result.exit_code,
            finished_at=finished,
            duration_ms=duration,
            commit_sha=sha,
        )
        task_service.block(
            session,
            task,
            "worker emitted BLOCKED handoff: " + "; ".join(handoff.blockers),
            role="supervisor",
        )
        return {
            "run_id": run.id,
            "status": run.status,
            "decision": "blocked",
            "blockers": handoff.blockers,
        }

    _set_run(session, run, RunStatus.testing, exit_code=result.exit_code, commit_sha=sha)
    task_service.transition(session, task, TaskStatus.review, "handoff received", role="supervisor")
    try:
        outcome = await supervisor_service.after_run(
            session, task, run, project, handoff, synthesized
        )
    finally:
        finished = utcnow()
        session.refresh(run)
        if run.status == RunStatus.testing:
            _set_run(
                session,
                run,
                RunStatus.completed,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
            )
    return {"run_id": run.id, "status": run.status, **outcome}


async def _execute_direct(
    session: Session, task: Task, run: Run, project: Project | None, profile_override: str | None
) -> dict[str, Any]:
    """Agent tasks (and harness-less low-risk tasks): call the model directly.
    Output is stored as an artifact; no code is written to any repository."""
    assert run.id is not None
    if profile_override:
        task.assigned_provider_profile = profile_override
    role = Role.agent if task.kind == TaskKind.agent else Role.worker
    choice = provider_service.choose_profile(session, task, role)
    prompt = prompt_builder.build_prompt(
        session, role, task, project, DynamicState(feedback=_latest_feedback(session, task))
    )
    artifact_service.write_text(
        run.id, "prompt.md", prompt.static_text() + "\n\n" + prompt.dynamic_text()
    )
    _set_run(
        session,
        run,
        RunStatus.running,
        provider_profile=choice.profile.name,
        harness_adapter=None,
        command_preview=f"direct API: {choice.profile.model_name}",
    )
    try:
        handoff, res = await provider_service.complete_json(
            session,
            choice.profile,
            prompt,
            Handoff,
            metadata={
                "purpose": "agent",
                "tags": task.tags,
                "task_id": task.id,
                "phase": task_service.phase_label(task),
                "branch": task.branch_name or "",
            },
        )
    except ProviderError as e:
        finished = utcnow()
        _set_run(session, run, RunStatus.failed, error=redact(str(e)), finished_at=finished)
        task_service.transition(
            session, task, TaskStatus.planned, f"provider error: {e}", role="supervisor"
        )
        return {"run_id": run.id, "status": run.status, "error": str(e)}
    artifact_service.write_text(run.id, "stdout.log", res.text)
    artifact_service.write_json(run.id, "handoff.json", handoff.model_dump(mode="json"))
    artifact_service.write_text(run.id, "handoff.md", artifact_service.render_handoff_md(handoff))
    artifact_service.write_text(run.id, "patch.diff", "")
    finished = utcnow()
    run.usage = res.usage
    task.handoff_status = handoff.status
    if handoff.memory_suggestions:
        memory_service.suggest_from_handoff(
            session,
            handoff.memory_suggestions,
            source=f"agent:{run.short_id}",
            project_slug=project.slug if project else None,
            role=role,
        )
    _set_run(
        session,
        run,
        RunStatus.completed,
        exit_code=0,
        finished_at=finished,
        duration_ms=int((finished - (run.started_at or finished)).total_seconds() * 1000),
    )
    if handoff.status == "BLOCKED":
        task_service.block(session, task, "agent BLOCKED: " + "; ".join(handoff.blockers))
        return {"run_id": run.id, "status": run.status, "decision": "blocked"}
    task_service.transition(
        session, task, TaskStatus.review, "agent output received", role="supervisor"
    )
    outcome = await supervisor_service.after_run(session, task, run, project, handoff, False)
    return {"run_id": run.id, "status": run.status, **outcome}


async def run_task_now(
    session: Session, task: Task, harness: str | None = None, profile: str | None = None
) -> dict[str, Any]:
    """CLI/API helper: queue if needed, claim this exact task, and execute it."""
    from orchestrator.services import queue_service

    if task.status in (
        TaskStatus.draft,
        TaskStatus.planned,
        TaskStatus.rejected,
        TaskStatus.blocked,
    ):
        task_service.queue(session, task)
    if task.status != TaskStatus.queued:
        raise task_service.TaskError(f"task {task.short_id} is {task.status}; cannot run")
    if not queue_service.claim(session, task.id or 0):
        raise task_service.TaskError(f"task {task.short_id} was claimed by another runner")
    session.refresh(task)
    return await execute(session, task, harness, profile)


def cancel(session: Session, task: Task) -> dict[str, Any]:
    run = task_service.active_run(session, task)
    if run and run.id is not None:
        (artifact_service.run_dir(run.id) / process_service.CANCEL_MARKER).write_text("cancel\n")
        log_service.log_event(
            session,
            "cancel_requested",
            run.short_id,
            role="supervisor",
            task_id=task.id,
            run_id=run.id,
        )
        return {"cancel_requested": run.short_id}
    if task.status == TaskStatus.queued:
        task_service.transition(session, task, TaskStatus.planned, "cancelled before start")
        return {"dequeued": task.short_id}
    if task.status == TaskStatus.in_progress:
        # Claimed but no live run (e.g. crashed runner): release it.
        task_service.transition(session, task, TaskStatus.planned, "released stale claim")
        return {"released": task.short_id}
    raise task_service.TaskError(f"nothing to cancel for {task.short_id} ({task.status})")


def retry(
    session: Session, task: Task, harness: str | None = None, profile: str | None = None
) -> Task:
    if harness:
        task.assigned_harness_adapter = harness
    if profile:
        task.assigned_provider_profile = profile
    task.source = "retry"
    session.add(task)
    session.commit()
    if task.status in (TaskStatus.planned, TaskStatus.rejected, TaskStatus.blocked):
        return task_service.queue(session, task)
    raise task_service.TaskError(f"cannot retry a task in status {task.status}")


def escalate(session: Session, task: Task) -> Task:
    """Escalate: route the next run/review through the architect (Opus) profile."""
    tags = set(task.tags) | {"escalate"}
    task.tags = sorted(tags)
    task.risk = "high"
    task.source = "escalation"
    session.add(task)
    session.commit()
    log_service.log_event(
        session,
        "escalated",
        "risk=high; architect review required",
        role="supervisor",
        task_id=task.id,
    )
    return task


def human_decision(session: Session, task: Task, accept: bool, reason: str) -> dict[str, Any]:
    """Operator accept/reject of a task waiting in `review` (or reject from blocked)."""
    project = _project(session, task)
    if accept:
        run = task_service.latest_run(session, task)
        if task.status == TaskStatus.blocked and run is not None:
            task_service.transition(
                session, task, TaskStatus.review, "operator taking the decision", role="supervisor"
            )
        if task.status != TaskStatus.review or run is None:
            raise task_service.TaskError("only tasks in `review` with a run can be accepted")
        log_service.log_event(
            session, "human_accept", reason or "", role="supervisor", task_id=task.id
        )
        return supervisor_service.accept(session, task, run, project, None, None)
    task_service.transition(
        session, task, TaskStatus.rejected, reason or "rejected by human", role="supervisor"
    )
    return {"decision": "rejected"}


def run_summary(session: Session, run: Run) -> dict[str, Any]:
    data = json.loads(run.model_dump_json())
    data["artifacts"] = artifact_service.list_artifacts(run.id) if run.id else []
    return data
