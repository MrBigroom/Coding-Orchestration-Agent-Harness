"""Standalone agent runs: a harness session with nobody watching, outside the task pipeline.

A run has no task, Supervisor or review. It reuses the Chat session drivers, so its
transcript opens on the Chat page, but no one can answer an approval, so `decide` answers
them from the run's access level. Each agent has an output folder under
<home>/agent-outputs, and each run saves its final message there as a Markdown report.

Access `read` works in a detached snapshot of the project's base branch, removed afterwards.
Access `write` works on its own branch, kept for review. The limits are the harness's
permission prompts, except on DeepSeek Harness, which has an OS sandbox.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.chat.base import DriverError
from orchestrator.adapters.chat.registry import HARNESSES
from orchestrator.core.config import get_settings
from orchestrator.core.db import session_scope
from orchestrator.core.logging import get_logger
from orchestrator.core.models import (
    Agent,
    ChatSession,
    Project,
    Role,
    StandaloneRun,
    StandaloneStatus,
    TaskKind,
    utcnow,
)
from orchestrator.providers.base import ProviderError
from orchestrator.services import (
    adapter_service,
    agent_service,
    chat_service,
    log_service,
    provider_service,
    settings_service,
)
from orchestrator.utils.git import GitError
from orchestrator.utils.paths import is_within
from orchestrator.utils.redaction import redact
from orchestrator.utils.slug import slugify

log = get_logger(__name__)
MAX_STANDALONE_RUNS = 2
GRACE_SECONDS = 10
MAX_NUDGES = 3
GATE_BLOCK = "This session is read-only."  # from pi_gate.ts, which blocks without asking
NUDGE = (
    "That action was denied, because this run's access doesn't allow it. Continue without it "
    "using what you're allowed to do, then finish with your report."
)
_watchers: set[asyncio.Task] = set()

ACCESS_NOTE = {
    "read": (
        "You may read the project, but you may not change it or run commands that change "
        "anything. Actions that aren't allowed are denied automatically."
    ),
    "write": (
        "You may change files in your working directory and run commands. Your changes stay "
        "on a separate branch for review. Do not touch anything outside it and the output "
        "folder."
    ),
}


class StandaloneError(ValueError):
    pass


def output_folder(agent: Agent) -> Path:
    return get_settings().agent_outputs_root / (slugify(agent.name) or f"agent-{agent.id}")


def build_prompt(agent: Agent, request: str, access: str, output: Path) -> str:
    parts = []
    if agent.instructions.strip():
        parts.append(f"Agent instructions ({agent.name}):\n{agent.instructions.strip()}")
    parts.append(f"Request:\n{request.strip()}")
    parts.append(
        "This is a scheduled run and nobody is watching it, so you can't ask questions or "
        f"wait for approval. {ACCESS_NOTE[access]}\n"
        f"Output folder: {output}\nSave any files worth keeping there. Your final message "
        "becomes the saved report, so make it complete: what you did, what you found, and "
        "anything you couldn't do."
    )
    return "\n\n".join(parts)


def decide(access: str, action: dict[str, Any] | None, cwd: Path, output: Path) -> bool:
    """Answer an approval. Anything the policy can't identify is denied."""
    kind = (action or {}).get("kind")
    if kind == "read":
        return True
    if kind == "write":
        roots = [output] if access == "read" else [cwd, output]
        paths = [(cwd / p).resolve() for p in action.get("paths") or []] if action else []
        return bool(paths) and all(
            any(p == root.resolve() or is_within(root.resolve(), p) for root in roots)
            for p in paths
        )
    return access == "write"


def _pick(options: list[dict[str, str]], tone: str) -> str | None:
    matching = [o for o in options if o.get("tone") == tone]
    once = [o for o in matching if "always" not in f"{o['id']} {o['label']}".lower()]
    return (once or matching or [{}])[0].get("id")


def _harness(session: Session, agent: Agent, profile: Any) -> str:
    routes = adapter_service.harness_routes(session)
    route = adapter_service.route_for(routes, profile.name, profile.model_name)
    names = [agent.harness_adapter] if agent.harness_adapter else [*route, "opencode"]
    problems = []
    for name in dict.fromkeys(names):
        try:
            problem = chat_service.harness_problem(session, name)
        except KeyError:
            continue
        if problem is None:
            return name
        problems.append(f"{name}: {problem}")
    raise StandaloneError("no harness can run this agent. " + " ".join(problems))


def _route(session: Session, agent: Agent) -> tuple[str, str]:
    draft = agent_service._draft(agent)
    role = Role.agent if draft.kind == TaskKind.agent else Role.worker
    profile = provider_service.choose_profile(session, draft, role).profile
    return _harness(session, agent, profile), profile.model_name


def _skip_reason(
    session: Session, agent: Agent, schedule_id: int | None, run_id: int | None
) -> str | None:
    if not settings_service.get(session, "allow_unattended_sessions"):
        return "standalone runs are off (allow_unattended_sessions=false)"
    if not agent.enabled:
        return f"agent {agent.name!r} is disabled"
    if chat_service.manager.unattended_count() >= MAX_STANDALONE_RUNS:
        return f"at most {MAX_STANDALONE_RUNS} standalone runs can run at once"
    if schedule_id is not None:
        running = session.exec(
            select(StandaloneRun).where(
                StandaloneRun.schedule_id == schedule_id,
                StandaloneRun.status == StandaloneStatus.running,
                col(StandaloneRun.id) != run_id,
            )
        ).first()
        if running:
            return f"the previous run (#{running.id}) is still going"
    return None


def _end(session: Session, run: StandaloneRun, status: str, error: str | None) -> StandaloneRun:
    run.status = status
    run.error = redact(error) if error else None
    run.finished_at = utcnow()
    session.add(run)
    session.commit()
    detail = f"{run.title}: {status}" + (f" ({run.error})" if run.error else "")
    log_service.log_event(session, "standalone_run", detail)
    return run


async def launch(
    session: Session,
    agent: Agent,
    request: str,
    *,
    schedule_id: int | None = None,
    access: str = "read",
    time_limit_minutes: int = 30,
) -> StandaloneRun:
    """Start a run. Reasons not to run are recorded on the run, so they show up in Outputs."""
    first_line = request.strip().splitlines()[0][:80]
    run = StandaloneRun(
        agent_id=agent.id or 0,
        schedule_id=schedule_id,
        title=redact(f"[{agent.name}] {first_line}"),
        access=access,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    if reason := _skip_reason(session, agent, schedule_id, run.id):
        return _end(session, run, StandaloneStatus.skipped, reason)

    output = output_folder(agent)
    output.mkdir(parents=True, exist_ok=True)
    project = session.get(Project, agent.project_id) if agent.project_id else None
    workspace = "folder"
    if project:
        workspace = "snapshot" if access == "read" else "worktree"
    try:
        run.adapter, run.model = _route(session, agent)
        chat = await chat_service.open_session(
            session,
            adapter=run.adapter,
            project=project,
            model=run.model,
            permission=HARNESSES[adapter_service.get_row(session, run.adapter).kind].unattended[
                access
            ],
            workspace=workspace,
            title=run.title,
            folder=output,
            standalone=True,
            output_dir=output,
        )
    except (ValueError, ProviderError, GitError, KeyError, OSError) as e:
        return _end(session, run, StandaloneStatus.failed, str(e))
    run.chat_id = chat.id
    session.add(run)
    session.commit()
    live = chat_service.manager.get(chat.id or 0)
    assert live
    queue = live.subscribe()  # before anything can yield, so a failed start isn't missed
    watcher = asyncio.create_task(
        _watch(live, queue, run.id or 0, access, Path(chat.cwd), output, time_limit_minutes * 60)
    )
    _watchers.add(watcher)
    watcher.add_done_callback(_watchers.discard)
    live.spawn(live.prompt(build_prompt(agent, request, access, output)))
    return run


async def _answer(
    live: chat_service.LiveSession,
    event: dict[str, Any],
    allow: bool,
) -> None:
    choice = _pick(event.get("options") or [], "allow" if allow else "deny")
    try:
        if choice and live.driver:
            await live.driver.respond(event["id"], choice)
        elif not allow:
            await live.interrupt()  # no way to refuse it, so end the turn instead
    except DriverError as e:
        log.warning("chat %s: could not answer an approval: %s", live.id, e)


async def _watch(
    live: chat_service.LiveSession,
    queue: asyncio.Queue,
    run_id: int,
    access: str,
    cwd: Path,
    output: Path,
    limit_seconds: float,
) -> None:
    """Follow a run's events until the agent finishes, then save the outcome. Some harnesses
    end the turn when an action is denied, so a turn that stops there gets a nudge to carry
    on and write its report."""
    started = time.time()
    deadline = time.monotonic() + limit_seconds
    everything, latest = "", ""
    blocked: list[str] = []
    errors: list[str] = []
    tools: dict[str, str] = {}
    busy = False
    denied = False
    nudges = 0
    status = StandaloneStatus.stopped
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), max(0, deadline - time.monotonic()))
        except TimeoutError:
            status = StandaloneStatus.timeout
            errors.append(f"Stopped at the time limit ({limit_seconds // 60:.0f} minutes).")
            break
        if event is None:
            break
        kind = event["type"]
        if kind == "text":
            everything += event["delta"]
            latest += event["delta"]
        elif kind == "tool_start":
            latest = ""
            tools[event["id"]] = f"{event.get('name')} {event.get('input') or ''}".strip()
        elif kind == "tool_end":
            latest = ""
            if event.get("error") and GATE_BLOCK in str(event.get("output")):
                blocked.append(tools.get(event["id"], "A tool call")[:300])
        elif kind == "error":
            errors.append(event["message"])
        elif kind == "approval":
            allow = decide(access, event.get("action"), cwd, output)
            if not allow:
                denied = True
                blocked.append(f"{event.get('title')} {event.get('detail') or ''}".strip()[:300])
            await _answer(live, event, allow)
        elif kind == "status":
            if event["status"] == "busy":
                busy = True
            elif event["status"] == "failed":
                status = StandaloneStatus.failed
            elif event["status"] == "idle" and busy and not live.pending:
                if denied and not latest.strip() and nudges < MAX_NUDGES:
                    nudges += 1
                    denied = busy = False
                    # Not awaited: an ACP prompt returns when its turn ends, and this loop
                    # has to keep answering that turn's approvals.
                    live.spawn(live.prompt(NUDGE))
                    continue
                status = StandaloneStatus.completed
                break
    await live.shutdown()
    if status == StandaloneStatus.completed and errors and not latest.strip():
        status = StandaloneStatus.failed
    _save_outcome(run_id, status, latest.strip() or everything.strip(), blocked, errors, started)


def _new_files(output: Path, since: float) -> list[str]:
    files = (p for p in output.rglob("*") if p.is_file() and "reports" not in p.parts)
    return sorted(str(p.relative_to(output)) for p in files if p.stat().st_mtime >= since - 1)


def _report_text(run: StandaloneRun, report: str, blocked: list[str], errors: list[str]) -> str:
    parts = [
        f"# {run.title}",
        f"Status: {run.status}  \nStarted: {run.started_at:%Y-%m-%d %H:%M} UTC  \n"
        f"Harness: {run.adapter} ({run.model or 'its own default'})  \nAccess: {run.access}",
        report or "The agent didn't write a report.",
    ]
    if blocked:
        parts.append("## Blocked\n\n" + "\n".join(f"- {b}" for b in blocked))
    if errors:
        parts.append("## Errors\n\n" + "\n".join(f"- {e}" for e in errors))
    return redact("\n\n".join(parts)) + "\n"


def _remove_snapshot(session: Session, chat: ChatSession) -> str | None:
    project = session.get(Project, chat.project_id or 0)
    if chat.workspace != "snapshot" or not project:
        return None
    outcome = chat_service.remove_worktree(project, chat)
    return outcome if outcome.startswith("skip") else None


def _save_outcome(
    run_id: int,
    status: str,
    report: str,
    blocked: list[str],
    errors: list[str],
    started: float,
) -> None:
    with session_scope() as session:
        run = session.get(StandaloneRun, run_id)
        agent = session.get(Agent, run.agent_id) if run else None
        if not run or not agent:
            return
        chat = session.get(ChatSession, run.chat_id or 0)
        run.status = status
        run.blocked = [redact(b) for b in blocked]
        run.summary = redact(" ".join(report.split()))[:300]
        if chat:
            run.usage = chat.usage
            if leftover := _remove_snapshot(session, chat):
                errors = [*errors, f"The read-only snapshot was changed and kept: {leftover}"]
        output = output_folder(agent)
        run.files = _new_files(output, started)
        path = output / "reports" / f"{run.started_at:%Y-%m-%d-%H%M}-run{run.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_report_text(run, report, blocked, errors), encoding="utf-8")
        run.report_path = str(path)
        _end(session, run, status, errors[-1] if errors else None)


def list_runs(session: Session, agent: Agent, limit: int = 30) -> list[StandaloneRun]:
    newest = col(StandaloneRun.id).desc()
    query = select(StandaloneRun).where(StandaloneRun.agent_id == agent.id)
    return list(session.exec(query.order_by(newest).limit(limit)))


def get(session: Session, run_id: int) -> StandaloneRun:
    run = session.get(StandaloneRun, run_id)
    if not run:
        raise KeyError(f"standalone run {run_id} not found")
    return run


def read_report(run: StandaloneRun) -> str:
    path = Path(run.report_path or "")
    root = get_settings().agent_outputs_root
    if not run.report_path or not path.is_file() or not is_within(root, path):
        raise KeyError(f"run {run.id} has no report")
    return path.read_text(encoding="utf-8")


async def stop(run: StandaloneRun) -> None:
    if run.status != StandaloneStatus.running:
        raise StandaloneError("this run has already ended")
    await chat_service.close(run.chat_id or 0)
    for _ in range(GRACE_SECONDS * 10):  # the watcher saves the outcome
        if not _watchers:
            return
        await asyncio.sleep(0.1)


def recover(session: Session) -> None:
    """Runs can't outlive the server, so any still marked running were cut off."""
    query = select(StandaloneRun).where(StandaloneRun.status == StandaloneStatus.running)
    for run in session.exec(query):
        if chat := session.get(ChatSession, run.chat_id or 0):
            _remove_snapshot(session, chat)
        run.status = StandaloneStatus.interrupted
        run.error = "The server stopped during this run."
        run.finished_at = utcnow()
        session.add(run)
    session.commit()
