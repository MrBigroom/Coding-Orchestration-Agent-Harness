"""`orchestrator` command-line interface (Typer + Rich). Fully usable without the GUI."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import webbrowser
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from orchestrator.core.config import get_settings
from orchestrator.core.logging import configure_logging

app = typer.Typer(help="Local-first coding orchestration harness.", no_args_is_help=True)
project_app = typer.Typer(help="Manage projects.", no_args_is_help=True)
task_app = typer.Typer(help="Create, run, and gate tasks.", no_args_is_help=True)
adapters_app = typer.Typer(help="External harness adapters.", no_args_is_help=True)
providers_app = typer.Typer(help="Model providers.", no_args_is_help=True)
models_app = typer.Typer(help="Model profiles.", no_args_is_help=True)
schedule_app = typer.Typer(help="Cron and one-time schedules.", no_args_is_help=True)
memory_app = typer.Typer(help="Shared memory.", no_args_is_help=True)
agent_app = typer.Typer(help="Saved agents.", no_args_is_help=True)
sync_app = typer.Typer(help="Supabase cloud synchronization.", no_args_is_help=True)
desktop_app = typer.Typer(
    help="Desktop app: run it (no subcommand), install/uninstall the launcher, stop the server."
)
for sub, name in (
    (project_app, "project"),
    (task_app, "task"),
    (adapters_app, "adapters"),
    (providers_app, "providers"),
    (models_app, "models"),
    (schedule_app, "schedule"),
    (memory_app, "memory"),
    (agent_app, "agent"),
    (sync_app, "sync"),
    (desktop_app, "desktop"),
):
    app.add_typer(sub, name=name)

console = Console()
err = Console(stderr=True)


def _session():
    from orchestrator.core.db import session_scope

    return session_scope()


def handled(fn: Callable) -> Callable:
    """Turn expected domain errors into a clean message + exit code 1."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from orchestrator.core.state import InvalidTransition
        from orchestrator.providers.base import ProviderError
        from orchestrator.utils.git import GitError

        try:
            return fn(*args, **kwargs)
        except (
            KeyError,
            ValueError,
            InvalidTransition,
            GitError,
            ProviderError,
            FileNotFoundError,
        ) as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            err.print(f"[red]error:[/red] {msg}")
            raise typer.Exit(1) from e

    return wrapper


def _ensure_ready() -> None:
    """Commands that touch the DB need migrations applied; do it transparently."""
    from orchestrator.core import db

    get_settings().ensure_dirs()
    if db.current_revision() is None:
        from orchestrator.services import bootstrap

        bootstrap.init()
    else:
        db.upgrade()


def _print_json(data: Any) -> None:
    console.print_json(json.dumps(data, default=str))


def _status_style(status: str) -> str:
    return {
        "accepted": "green",
        "completed": "green",
        "pass": "green",
        "APPROVED": "green",
        "queued": "cyan",
        "in_progress": "yellow",
        "running": "yellow",
        "review": "magenta",
        "blocked": "red",
        "failed": "red",
        "rejected": "red",
        "timeout": "red",
        "CHANGES_REQUESTED": "yellow",
        "NEEDS_HUMAN": "red",
    }.get(status, "white")


# ---------------------------------------------------------------- top level


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    configure_logging("DEBUG" if verbose else get_settings().log_level)


@app.command()
@handled
def init() -> None:
    """Create ORCHESTRATOR_HOME, run migrations, seed providers/adapters/memory."""
    from orchestrator.services import bootstrap

    info = bootstrap.init()
    console.print(f"[green]initialised[/green] home={info['home']} revision={info['revision']}")


@app.command()
@handled
def doctor() -> None:
    """Check environment, database, providers, and adapters."""
    from orchestrator.services import bootstrap

    get_settings().ensure_dirs()
    table = Table("check", "status", "detail")
    worst = 0
    for c in bootstrap.doctor():
        mark = {
            "ok": "[green]ok[/green]",
            "warn": "[yellow]warn[/yellow]",
            "error": "[red]FAIL[/red]",
        }[c["level"]]
        worst = max(worst, {"ok": 0, "warn": 0, "error": 1}[c["level"]])
        table.add_row(c["name"], mark, str(c["detail"]))
    console.print(table)
    raise typer.Exit(worst)


@app.command()
def serve(
    host: str = typer.Option(None, help="Bind host (default from settings)."),
    port: int = typer.Option(None, help="Bind port."),
    reload: bool = typer.Option(False, help="Auto-reload (development)."),
) -> None:
    """Run the API + GUI server."""
    import uvicorn

    s = get_settings()
    host = host or s.bind_host
    port = port or s.bind_port
    from orchestrator.core.config import LOCAL_HOSTS

    if host not in LOCAL_HOSTS:
        err.print(
            f"[bold red]WARNING:[/bold red] binding to {host} exposes the orchestrator "
            "beyond this machine."
        )
        if not s.token:
            err.print(
                "[red]refusing: set ORCHESTRATOR_TOKEN when binding to a non-local host.[/red]"
            )
            raise typer.Exit(2)
    _ensure_ready()
    if s.autostart_browser:
        webbrowser.open(f"http://{host}:{port}/")
    uvicorn.run(
        "orchestrator.api.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level=s.log_level.lower(),
    )


@desktop_app.callback(invoke_without_command=True)
def desktop(
    ctx: typer.Context,
    debug: bool = typer.Option(False, help="Enable the web inspector."),
) -> None:
    """Open the GUI in a native window (starts or attaches to the server)."""
    if ctx.invoked_subcommand:
        return
    from orchestrator import desktop as d

    try:
        d.run(debug=debug)
    except d.DesktopError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(1) from e


@desktop_app.command("install")
def desktop_install() -> None:
    """Add the app-menu launcher and icon for the current user (~/.local/share)."""
    from orchestrator import desktop as d

    for path in d.install(Path.cwd()):
        console.print(f"[green]installed[/green] {path}")
    console.print(f"Working directory for .env: {Path.cwd()}")


@desktop_app.command("uninstall")
def desktop_uninstall() -> None:
    """Remove the launcher and icon."""
    from orchestrator import desktop as d

    removed = d.uninstall()
    for path in removed:
        console.print(f"removed {path}")
    if not removed:
        console.print("nothing installed")


@desktop_app.command("stop")
def desktop_stop() -> None:
    """Stop a server the desktop app left running in the background."""
    from orchestrator import desktop as d

    console.print("stopped" if d.stop_background() else "no background server running")


@app.command("open")
def open_gui() -> None:
    """Open the GUI in your browser."""
    s = get_settings()
    url = f"http://{s.bind_host}:{s.bind_port}/"
    console.print(url)
    webbrowser.open(url)


@app.command()
@handled
def demo(tags: list[str] = typer.Option([], "--tag", help="Extra tags, e.g. mock-reject.")) -> None:
    """Run the full role loop offline (mock provider + mock harness + sample project)."""
    from orchestrator.services import demo_service

    get_settings().ensure_dirs()
    with _session() as s:
        result = asyncio.run(demo_service.run_demo(s, tags))
    for step in result["steps"]:
        console.print(f"[cyan]•[/cyan] {step}")
    style = _status_style(result["status"])
    console.print(f"\nTask {result['task']} -> [{style}]{result['status']}[/{style}]")


@app.command("prompt-preview")
@handled
def prompt_preview(
    role: str = typer.Option("worker", help="architect|supervisor|worker|agent"),
    task: str = typer.Option(None, help="Task id or short id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show prompt sections (static vs dynamic), token estimates, and ordering warnings."""
    from orchestrator.core.models import Project
    from orchestrator.services import prompt_builder, task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.get(s, task) if task else None
        p = s.get(Project, t.project_id) if t and t.project_id else None
        built = prompt_builder.build_prompt(s, role, t, p)
        report = prompt_builder.preview(built.sections, built)
    if as_json:
        _print_json(report)
        return
    table = Table("#", "section", "kind", "breakpoint", "~tokens")
    for r in report["sections"]:
        kind = "[green]static[/green]" if r["static"] else "[yellow]dynamic[/yellow]"
        table.add_row(
            str(r["index"]),
            r["name"],
            kind,
            "✓" if r["cache_breakpoint"] else "",
            "-" if r["empty"] else str(r["tokens"]),
        )
    console.print(table)
    console.print(
        f"static ≈{report['static_tokens']} tokens, dynamic ≈{report['dynamic_tokens']}"
        f", cacheable {report['cacheable_fraction']:.0%}"
    )
    for w in report["warnings"]:
        console.print(f"[red]warning:[/red] {w}")


@app.command()
@handled
def cleanup(
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Only show what would be removed."
    ),
    include_active: bool = typer.Option(
        False, help="Also remove worktrees of tasks not accepted/archived/rejected."
    ),
) -> None:
    """Remove orchestrator-managed worktrees (branches are never deleted)."""
    from sqlmodel import select

    from orchestrator.core.models import Project, Task
    from orchestrator.services import git_service

    _ensure_ready()
    done = {"accepted", "archived", "rejected"}
    # T-20: parity residue is not a task worktree and has its own lock-aware sweep. Doing it here
    # (as well as at the start of the parity probe) means a SIGKILLed run is reclaimable without
    # waiting for the next doctor invocation.
    for action in git_service.sweep_parity_residue(dry_run=dry_run):
        console.print(action.detail)
    with _session() as s:
        projects = {p.slug: p for p in s.exec(select(Project))}
        for info in git_service.managed_worktrees():
            if info["task_id"] == git_service.PARITY_TASK_ID:
                # Already handled above; a live parity snapshot was skipped there on purpose.
                continue
            task = s.get(Task, int(info["task_id"])) if info["task_id"].isdigit() else None
            if task and task.status not in done and not include_active:
                console.print(f"skip {info['path']}: task {task.short_id} is {task.status}")
                continue
            proj = projects.get(info["project_slug"])
            msg = git_service.remove_worktree(
                Path(proj.path) if proj else None, info, dry_run=dry_run
            )
            console.print(msg)
    if dry_run:
        console.print("[dim](dry run — pass --no-dry-run to apply)[/dim]")


@app.command()
def reset(
    i_understand_data_loss: bool = typer.Option(
        False, "--i-understand-data-loss", help="Required. Deletes DB, runs, logs, memory."
    ),
) -> None:
    """Delete ORCHESTRATOR_HOME data (database, run artifacts, logs, memory, worktrees)."""
    s = get_settings()
    if not i_understand_data_loss:
        err.print(
            "refusing: pass --i-understand-data-loss to delete "
            f"{s.home} (your project repositories are not touched)"
        )
        raise typer.Exit(2)
    from orchestrator.core.db import dispose_engines

    dispose_engines()
    for p in (
        s.home / "orchestrator.db",
        s.home / "orchestrator.db-wal",
        s.home / "orchestrator.db-shm",
        s.build_log_path,
    ):
        p.unlink(missing_ok=True)
    for d in (
        s.artifact_root,
        s.log_root,
        s.memory_root,
        s.worktree_root,
        s.home / "sample-project",
    ):
        if d and d.exists():
            shutil.rmtree(d)
    console.print(
        f"[yellow]reset[/yellow] {s.home}. Branches in your repositories were kept; "
        "run `git worktree prune` in them if needed."
    )


# ---------------------------------------------------------------- project


@project_app.command("add")
@handled
def project_add(
    name: str,
    path: Path,
    base_branch: str = typer.Option(None),
    test_command: str = typer.Option(None),
    lint_command: str = typer.Option(None),
    build_command: str = typer.Option(None),
    allow: list[str] = typer.Option([], "--allow", help="Allowed command/executable."),
    notes: str = typer.Option(None),
) -> None:
    from orchestrator.core.schemas import ProjectCreate
    from orchestrator.services import project_service

    _ensure_ready()
    with _session() as s:
        p = project_service.create(
            s,
            ProjectCreate(
                name=name,
                path=str(path),
                base_branch=base_branch,
                test_command=test_command,
                lint_command=lint_command,
                build_command=build_command,
                allowed_commands=allow,
                notes=notes,
            ),
        )
        console.print(
            f"[green]added[/green] project #{p.id} {p.name} ({p.path}, base {p.base_branch})"
        )


@project_app.command("list")
@handled
def project_list() -> None:
    from sqlmodel import select

    from orchestrator.core.models import Project

    _ensure_ready()
    with _session() as s:
        table = Table("id", "name", "path", "base", "test")
        for p in s.exec(select(Project)):
            table.add_row(str(p.id), p.name, p.path, p.base_branch, p.test_command or "-")
    console.print(table)


@project_app.command("doctor")
@handled
def project_doctor(
    project: str,
    run_commands: bool = typer.Option(
        False,
        "--run-commands",
        help="Opt-in: execute the configured test/lint/build in a throwaway worktree of the "
        "base branch and check they exit 0 AND resolve imports inside that worktree. Runs "
        "project code, so it is off by default and is never reachable from the API or GUI.",
    ),
    env_parity: bool = typer.Option(
        False,
        "--env-parity",
        help="Developer check, NOT the gate: run the configured TEST command in a throwaway "
        "worktree with a copy of the local .env seeded in, so you can see whether ambient "
        "configuration changes the result. Contents are never printed or committed.",
    ),
) -> None:
    from orchestrator.services import project_service

    _ensure_ready()
    with _session() as s:
        checks = project_service.doctor(
            project_service.get(s, project), run_commands=run_commands, env_parity=env_parity
        )
    bad = 0
    for c in checks:
        bad += 0 if c["ok"] else 1
        console.print(
            f"{'[green]ok[/green]  ' if c['ok'] else '[red]FAIL[/red]'} {c['name']}"
            f"{' — ' + c['detail'] if c['detail'] else ''}"
        )
    raise typer.Exit(1 if bad else 0)


# ---------------------------------------------------------------- task


def _task_row(t) -> list[str]:
    style = _status_style(t.status)
    return [
        t.short_id,
        f"[{style}]{t.status}[/{style}]",
        t.kind,
        t.risk,
        t.title,
        ",".join(t.tags),
        t.branch_name or "-",
    ]


@task_app.command("create")
@handled
def task_create(
    title: str,
    project: str = typer.Option(None, help="Project id/name."),
    description: str = typer.Option("", "--description", "-d"),
    kind: str = typer.Option("coding"),
    risk: str = typer.Option("low"),
    priority: str = typer.Option("normal"),
    tag: list[str] = typer.Option([], "--tag", "-t"),
    phase: int = typer.Option(None, help="Phase number."),
    slug: str = typer.Option(None, help="Phase slug."),
    dod: list[str] = typer.Option([], "--dod", help="Definition of Done item."),
    criteria: list[str] = typer.Option([], "--criteria"),
    contract: list[str] = typer.Option([], "--contract", help="Frozen contract."),
    out_of_scope: list[str] = typer.Option([], "--out-of-scope"),
    harness: str = typer.Option(None),
    profile: str = typer.Option(None),
    queue: bool = typer.Option(False, help="Queue immediately."),
) -> None:
    from orchestrator.core.schemas import TaskCreate
    from orchestrator.services import project_service, task_service

    _ensure_ready()
    with _session() as s:
        pid = project_service.get(s, project).id if project else None
        t = task_service.create(
            s,
            TaskCreate(
                title=title,
                project_id=pid,
                description=description,
                kind=kind,
                risk=risk,
                priority=priority,
                tags=tag,
                phase_number=phase,
                phase_slug=slug,
                definition_of_done=dod,
                acceptance_criteria=criteria,
                frozen_contracts=contract,
                out_of_scope=out_of_scope,
                assigned_harness_adapter=harness,
                assigned_provider_profile=profile,
                queue=queue,
            ),
        )
        console.print(
            f"[green]created[/green] {t.short_id} ({t.status})"
            + (f" branch {t.branch_name}" if t.branch_name else "")
        )


@task_app.command("list")
@handled
def task_list(
    status: str = typer.Option(None),
    project: str = typer.Option(None),
    kind: str = typer.Option(None),
    tag: str = typer.Option(None),
    all_: bool = typer.Option(False, "--all", help="Include archived."),
) -> None:
    from orchestrator.services import project_service, task_service

    _ensure_ready()
    with _session() as s:
        pid = project_service.get(s, project).id if project else None
        tasks = task_service.list_tasks(
            s, project_id=pid, status=status, kind=kind, tag=tag, include_archived=all_
        )
        table = Table("id", "status", "kind", "risk", "title", "tags", "branch")
        for t in tasks:
            table.add_row(*_task_row(t))
    console.print(table)


@task_app.command("show")
@handled
def task_show(task: str, as_json: bool = typer.Option(False, "--json")) -> None:
    from orchestrator.services import task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.get(s, task)
        runs = task_service.runs_for(s, t)
        data = json.loads(t.model_dump_json())
        data["runs"] = [json.loads(r.model_dump_json()) for r in runs]
    if as_json:
        _print_json(data)
        return
    console.print(f"[bold]{t.short_id}[/bold] {t.title}")
    for k in (
        "status",
        "kind",
        "risk",
        "priority",
        "tags",
        "branch_name",
        "handoff_status",
        "review_status",
        "verification_status",
        "merge_status",
        "source",
    ):
        console.print(f"  {k}: {data.get(k)}")
    if t.definition_of_done:
        console.print("  DoD:")
        for i, d in enumerate(t.definition_of_done, 1):
            console.print(f"    {i}. {d}")
    table = Table("run", "status", "adapter", "profile", "exit", "ms", "artifacts")
    for r in runs:
        table.add_row(
            r.short_id,
            r.status,
            r.harness_adapter or "-",
            r.provider_profile or "-",
            str(r.exit_code),
            str(r.duration_ms),
            r.artifacts_path or "-",
        )
    console.print(table)


@task_app.command("run")
@handled
def task_run(
    task: str, harness: str = typer.Option(None), profile: str = typer.Option(None)
) -> None:
    """Queue (if needed), claim, and execute one task synchronously."""
    from orchestrator.services import run_service, task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.get(s, task)
        outcome = asyncio.run(run_service.run_task_now(s, t, harness, profile))
        s.refresh(t)
    _print_json(outcome)
    style = _status_style(t.status)
    console.print(f"{t.short_id} -> [{style}]{t.status}[/{style}]")


@task_app.command("dry-run")
@handled
def task_dry_run(task: str) -> None:
    """Show provider/model/harness/command that would be used, without executing."""
    from orchestrator.services import run_service, task_service

    _ensure_ready()
    with _session() as s:
        plan = run_service.dry_run(s, task_service.get(s, task))
    _print_json(plan.as_dict())


@task_app.command("cancel")
@handled
def task_cancel(task: str) -> None:
    from orchestrator.services import run_service, task_service

    _ensure_ready()
    with _session() as s:
        _print_json(run_service.cancel(s, task_service.get(s, task)))


@task_app.command("accept")
@handled
def task_accept(task: str, reason: str = typer.Option("", help="Recorded in build log.")) -> None:
    from orchestrator.services import run_service, task_service

    _ensure_ready()
    with _session() as s:
        _print_json(run_service.human_decision(s, task_service.get(s, task), True, reason))


@task_app.command("reject")
@handled
def task_reject(task: str, reason: str = typer.Option("", help="Recorded in build log.")) -> None:
    from orchestrator.services import run_service, task_service

    _ensure_ready()
    with _session() as s:
        _print_json(run_service.human_decision(s, task_service.get(s, task), False, reason))


@task_app.command("retry")
@handled
def task_retry(
    task: str,
    harness: str = typer.Option(None),
    profile: str = typer.Option(None),
    run: bool = typer.Option(False, "--run", help="Also execute now."),
) -> None:
    from orchestrator.services import run_service, task_service

    _ensure_ready()
    with _session() as s:
        t = run_service.retry(s, task_service.get(s, task), harness, profile)
        console.print(f"{t.short_id} -> {t.status}")
        if run:
            _print_json(asyncio.run(run_service.run_task_now(s, t)))


@task_app.command("block")
@handled
def task_block(task: str, reason: str = typer.Option(..., help="Why it is blocked.")) -> None:
    from orchestrator.services import task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.block(s, task_service.get(s, task), reason, role="supervisor")
        console.print(f"{t.short_id} -> {t.status}")


@task_app.command("unblock")
@handled
def task_unblock(task: str, reason: str = typer.Option("", help="Decision taken.")) -> None:
    from orchestrator.services import task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.unblock(s, task_service.get(s, task), reason)
        console.print(f"{t.short_id} -> {t.status}")


@task_app.command("archive")
@handled
def task_archive(task: str) -> None:
    from orchestrator.services import task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.archive(s, task_service.get(s, task))
        console.print(f"{t.short_id} -> {t.status}")


@task_app.command("escalate")
@handled
def task_escalate(task: str) -> None:
    """Mark high-risk and route review through the Architect profile."""
    from orchestrator.services import run_service, task_service

    _ensure_ready()
    with _session() as s:
        t = run_service.escalate(s, task_service.get(s, task))
        console.print(f"{t.short_id} escalated (risk={t.risk}, tags={t.tags})")


@task_app.command("merge")
@handled
def task_merge(task: str) -> None:
    """Merge an accepted task's branch with --no-ff (requires merge_enabled)."""
    from orchestrator.core.models import Project
    from orchestrator.services import supervisor_service, task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.get(s, task)
        p = s.get(Project, t.project_id) if t.project_id else None
        if not p:
            raise ValueError("task has no project")
        console.print(f"merged at {supervisor_service.manual_merge(s, t, p)}")


@task_app.command("plan")
@handled
def task_plan(task: str) -> None:
    """Ask the Architect profile to split a task into planned phase tasks."""
    from orchestrator.core.models import Project
    from orchestrator.services import planning_service, task_service

    _ensure_ready()
    with _session() as s:
        t = task_service.get(s, task)
        p = s.get(Project, t.project_id) if t.project_id else None
        created = asyncio.run(planning_service.plan_phases(s, t, p))
        for c in created:
            console.print(f"[green]planned[/green] {c.short_id} {c.title} ({c.branch_name})")


# ---------------------------------------------------------------- adapters/providers/models


@adapters_app.command("detect")
@handled
def adapters_detect() -> None:
    adapters_list()


@adapters_app.command("list")
@handled
def adapters_list() -> None:
    from sqlmodel import select

    from orchestrator.core.models import HarnessAdapter
    from orchestrator.services import adapter_service

    _ensure_ready()
    with _session() as s:
        table = Table("name", "enabled", "detected", "experimental", "command", "args")
        for row in s.exec(select(HarnessAdapter)):
            d = adapter_service.describe(row)
            table.add_row(
                d["name"],
                "yes" if d["enabled"] else "no",
                "[green]yes[/green]" if d["detected"] else "[dim]no[/dim]",
                "yes" if d["experimental"] else "no",
                d["command"],
                " ".join(d["args_template"]),
            )
    console.print(table)


@adapters_app.command("set")
@handled
def adapters_set(
    name: str,
    command: str = typer.Option(None),
    args: str = typer.Option(None, help="JSON list of argv template tokens."),
    enable: bool = typer.Option(None, "--enable/--disable"),
) -> None:
    """Edit an adapter's command template."""
    from orchestrator.core.schemas import AdapterUpdate
    from orchestrator.services import adapter_service

    _ensure_ready()
    with _session() as s:
        row = adapter_service.get_row(s, name)
        upd = AdapterUpdate(
            command=command, enabled=enable, args_template_json=json.loads(args) if args else None
        )
        for k, v in upd.model_dump(exclude_none=True).items():
            setattr(row, k, v)
        s.add(row)
        s.commit()
        _print_json(adapter_service.describe(row))


@providers_app.command("list")
@handled
def providers_list() -> None:
    from sqlmodel import select

    from orchestrator.core.models import Provider
    from orchestrator.services import provider_service

    _ensure_ready()
    with _session() as s:
        table = Table("name", "kind", "enabled", "key env", "key set", "configured", "notes")
        for p in s.exec(select(Provider)):
            st = provider_service.provider_status(p)
            table.add_row(
                p.name,
                p.kind,
                "yes" if p.enabled else "no",
                p.api_key_env or "-",
                "yes" if st["api_key_set"] else "no",
                "[green]yes[/green]"
                if st["configured"]
                else f"[yellow]{'; '.join(st['problems'])}[/yellow]",
                (p.notes or "")[:60],
            )
    console.print(table)


@models_app.command("list")
@handled
def models_list() -> None:
    from sqlmodel import select

    from orchestrator.core.models import ModelProfile, Provider

    _ensure_ready()
    with _session() as s:
        provs = {p.id: p.name for p in s.exec(select(Provider))}
        table = Table("name", "role", "provider", "model", "effort", "enabled")
        for m in s.exec(select(ModelProfile)):
            table.add_row(
                m.name,
                m.role,
                provs.get(m.provider_id, "?"),
                m.model_name,
                m.effort or "-",
                "yes" if m.enabled else "no",
            )
    console.print(table)


# ---------------------------------------------------------------- schedules


@schedule_app.command("add")
@handled
def schedule_add(
    name: str,
    cron: str = typer.Argument("", help="5-field cron. Leave out when using --at."),
    at: datetime | None = typer.Option(
        None,
        formats=["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"],
        help="Run once at this date and time.",
    ),
    timezone: str = typer.Option("UTC", help="IANA time zone for the cron or --at."),
    template: str = typer.Option("{}", help="Task template JSON (TaskCreate)."),
    agent: str | None = typer.Option(
        None, help="Run this saved agent; the template's description is the request."
    ),
    run_mode: str = typer.Option("pipeline", help="pipeline, or standalone (needs --agent)."),
    access: str = typer.Option("read", help="Standalone only: read or write."),
    time_limit: int = typer.Option(30, help="Standalone only: minutes before a run is stopped."),
    disabled: bool = typer.Option(False),
) -> None:
    from orchestrator.core.schemas import ScheduleCreate
    from orchestrator.services import agent_service, scheduler_service

    _ensure_ready()
    with _session() as s:
        agent_id = agent_service.get(s, agent).id if agent else None
        sc = scheduler_service.create(
            s,
            ScheduleCreate(
                name=name,
                cron_expression=cron,
                run_at=at,
                timezone=timezone,
                task_template_json=json.loads(template),
                agent_id=agent_id,
                run_mode=run_mode,
                access=access,
                time_limit_minutes=time_limit,
                enabled=not disabled,
            ),
        )
        console.print(
            f"[green]added[/green] schedule #{sc.id} {sc.name}; next run {sc.next_run_at}"
        )
        warning = scheduler_service.token_plan_warning(
            s, cron, agent_id, sc.task_template_json.get("assigned_provider_profile")
        )
        if warning:
            err.print(f"[yellow]warning:[/yellow] {warning}")


@schedule_app.command("list")
@handled
def schedule_list() -> None:
    from sqlmodel import select

    from orchestrator.core.models import Schedule

    _ensure_ready()
    with _session() as s:
        table = Table("id", "name", "when", "enabled", "last run", "next run")
        for sc in s.exec(select(Schedule)):
            table.add_row(
                str(sc.id),
                sc.name,
                sc.cron_expression or "once",
                "yes" if sc.enabled else "no",
                str(sc.last_run_at or "-"),
                str(sc.next_run_at or "-"),
            )
    console.print(table)


def _schedule_toggle(ref: str, enabled: bool) -> None:
    from orchestrator.core.schemas import ScheduleUpdate
    from orchestrator.services import scheduler_service

    _ensure_ready()
    with _session() as s:
        sc = scheduler_service.update(
            s, scheduler_service.get(s, ref), ScheduleUpdate(enabled=enabled)
        )
        console.print(f"schedule {sc.name}: enabled={sc.enabled}")


@schedule_app.command("enable")
@handled
def schedule_enable(schedule: str) -> None:
    _schedule_toggle(schedule, True)


@schedule_app.command("disable")
@handled
def schedule_disable(schedule: str) -> None:
    _schedule_toggle(schedule, False)


@schedule_app.command("run")
@handled
def schedule_run(schedule: str) -> None:
    """Fire a pipeline schedule now (creates and queues its task)."""
    from orchestrator.services import scheduler_service

    _ensure_ready()
    with _session() as s:
        t = scheduler_service.fire(s, scheduler_service.get(s, schedule))
        console.print(f"created {t.short_id} ({t.status})")


# ---------------------------------------------------------------- memory


def _memory_table(entries) -> Table:
    from orchestrator.services import memory_service

    table = Table("id", "category", "scope", "project", "role", "status", "inj", "content")
    for e in entries:
        status = "approved" if e.approved else ("rejected" if e.rejected else "pending")
        # T-4 DoD 4: `inj` marks an entry whose OWN category cannot reach its scope, so the
        # defect is visible the moment you list memory instead of months later. The full
        # reason is printed under the table (a Rich column squeezes long text to "UNINJECT…").
        # Valid role rows (category=role scope=worker, resolved through ROLE_MEMORY_KEY) are
        # never misflagged.
        ok, _reason = memory_service.injectability(e)
        # Everything below is operator data, not markup: a scope or a note containing
        # brackets would otherwise be eaten by Rich and the row would lie.
        table.add_row(
            str(e.id),
            escape(e.category),
            escape(e.scope),
            escape(e.project_slug or "-"),
            escape(e.role or "-"),
            status,
            "ok" if ok else "[red]NO[/red]",
            escape(e.content[:80]),
        )
    return table


def _print_injectability(entries) -> int:
    """Name every uninjectable row and return how many there were."""
    from orchestrator.services import memory_service

    bad = [e for e in entries if not memory_service.is_injectable(e)]
    for e in bad:
        console.print(
            f"[red]uninjectable[/red] #{e.id} {escape(e.category)}: "
            f"{escape(memory_service.injectability(e)[1])}"
        )
    if bad:
        err.print(
            f"{len(bad)} entr{'y' if len(bad) == 1 else 'ies'} above cannot be injected: "
            "fix with `orchestrator memory fix-scopes`."
        )
    return len(bad)


@memory_app.command("list")
@handled
def memory_list(
    status: str = typer.Option(None, help="approved|pending|rejected"),
    category: str = typer.Option(None),
    project: str = typer.Option(None),
    role: str = typer.Option(None),
    uninjectable: bool = typer.Option(
        False, "--uninjectable", help="Show only entries no prompt can read."
    ),
) -> None:
    from orchestrator.services import memory_service

    _ensure_ready()
    with _session() as s:
        entries = memory_service.list_entries(
            s, status=status, category=category, project_slug=project, role=role
        )
        if uninjectable:
            entries = [e for e in entries if not memory_service.is_injectable(e)]
    console.print(_memory_table(entries))
    _print_injectability(entries)


@memory_app.command("add")
@handled
def memory_add(
    content: str,
    category: str = typer.Option("global"),
    scope: str = typer.Option("preferences"),
    project: str = typer.Option(None),
    role: str = typer.Option(None),
    tag: list[str] = typer.Option([], "--tag"),
    approve: bool = typer.Option(False, help="Approve immediately."),
) -> None:
    from orchestrator.core.schemas import MemoryCreate
    from orchestrator.services import memory_service

    _ensure_ready()
    # `--role` is a plain string here, and MemoryCreate types it as the Role enum, so an
    # unreadable role would be refused by Pydantic with a message that never names the role
    # MEMORY FILES (DoD 2 requires the allowed values to be named). Validate it through the
    # service's own check first; a valid name (including the reviewer/agent aliases) passes
    # through untouched and is still re-checked by _validate.
    if role is not None:
        memory_service.check_role_name(role)
    with _session() as s:
        e = memory_service.create(
            s,
            MemoryCreate(
                category=category,
                scope=scope,
                content=content,
                project_slug=project,
                role=role,
                tags=tag,
                approved=approve,
                source="cli",
            ),
        )
        console.print(f"added memory #{e.id} ({'approved' if e.approved else 'pending'})")


@memory_app.command("approve")
@handled
def memory_approve(entry_id: int) -> None:
    from orchestrator.services import memory_service

    _ensure_ready()
    with _session() as s:
        e = memory_service.approve(s, entry_id)
        console.print(f"approved #{e.id}")


@memory_app.command("reject")
@handled
def memory_reject(entry_id: int) -> None:
    from orchestrator.services import memory_service

    _ensure_ready()
    with _session() as s:
        e = memory_service.reject(s, entry_id)
        console.print(f"rejected #{e.id}")


@memory_app.command("search")
@handled
def memory_search(query: str) -> None:
    from orchestrator.services import memory_service

    _ensure_ready()
    with _session() as s:
        found = memory_service.search(s, query)
    console.print(_memory_table(found))
    _print_injectability(found)


@memory_app.command("fix-scopes")
@handled
def memory_fix_scopes(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the moves, change nothing."),
    gc_only: bool = typer.Option(
        False, "--gc-only", help="Only remove rendered files that no entry owns."
    ),
    no_gc: bool = typer.Option(
        False,
        "--no-gc",
        help="Move rows and re-render, but delete no file (the GC list is still reported).",
    ),
) -> None:
    """Repair entries stored with a scope no injector can read (task T-4).

    Rows are moved, never deleted; rejected rows are left alone. Both the old and the new
    markdown file are re-rendered, and rendered files that no longer have an owning entry
    are removed so a re-scoped entry cannot leave a title-only ghost behind. Only files
    `render_file` could have written are collectable — hand-edited markdown in the memory
    tree is never deleted (see docs/memory.md), and `--no-gc` turns the deletion off.
    """
    from orchestrator.services import memory_service as m

    _ensure_ready()
    with _session() as s:
        if gc_only:
            if dry_run:
                # a preview of what a real run would collect: nothing is deleted, and the
                # verb printed below is "would remove"
                repair = m.ScopeRepair(removed_files=m.orphan_files(s), dry_run=True)
            else:
                # ONLY what was actually unlinked. `orphan_files()` is a pre-view taken
                # before the GC, and falling back to it (`removed or listed`) let a real run
                # print "removed dead scope-file" for files it had left on disk.
                repair = m.ScopeRepair(removed_files=m.gc_orphan_files(s), dry_run=False)
        else:
            repair = m.fix_scopes(s, dry_run=dry_run, gc=not no_gc)
    verb = "would move" if dry_run else "moved"
    file_verb = "would re-render" if dry_run else "re-rendered"
    for mv in repair.moved:
        console.print(
            f"{verb} #{mv['id']} {escape(str(mv['category']))}: "
            f"{escape(repr(mv['from']))} -> {escape(repr(mv['to']))}"
            + (f"  ({escape(mv['note'])})" if mv.get("note") else "")
        )
        console.print(
            f"  {file_verb} {escape(mv['old_file'])} and {escape(mv['new_file'])}"
        )
        # A dry run cannot show this file in the removed list below (the row has not moved
        # yet, so the file is still owned), so say here what the real run will collect.
        if dry_run and mv["collect_old_file"]:
            console.print(f"  would collect dead scope-file {escape(mv['old_file'])}")
    for sk in repair.skipped:
        console.print(
            f"left #{sk['id']} {escape(str(sk['category']))} scope "
            f"{escape(repr(sk['scope']))} alone: {escape(str(sk.get('why', '')))}"
        )
    ghost_verb = "would remove" if dry_run else "removed"
    for path in repair.removed_files:
        console.print(f"{ghost_verb} dead scope-file {escape(str(path))}")
    if repair.ghost_files:
        console.print(
            f"gc is off: {len(repair.ghost_files)} dead scope-file(s) left in place "
            f"({', '.join(escape(str(p)) for p in repair.ghost_files)})"
        )
    ids = repair.moved_ids
    console.print(
        f"{verb} {len(ids)} entr{'y' if len(ids) == 1 else 'ies'}"
        + (f" (ids: {', '.join(str(i) for i in ids)})" if ids else "")
        + f"; {len(repair.skipped)} left alone; "
        + (
            f"{len(repair.removed_files)} dead scope-file(s) "
            + ("to remove" if dry_run else "removed")
        )
        + (
            f"; {len(repair.ghost_files)} more reported with gc off"
            if repair.ghost_files
            else ""
        )
        + (" (dry run, nothing written)" if dry_run else "")
    )


_ACTION_STYLE = {
    "approve": "green",
    "merge": "cyan",
    "amend": "cyan",
    "split": "yellow",
    "rewrite-durable": "yellow",
    "verify-claims": "magenta",
    "fix-stale": "red",
    "reject": "red",
}


@memory_app.command("triage")
@handled
def memory_triage(
    status: str = typer.Option("pending", help="pending|approved|rejected"),
    project: str = typer.Option(None, help="Filter to one project slug."),
    role: str = typer.Option("worker", help="Role whose injection budget prices the queue."),
    action: str = typer.Option(None, "--action", help="Only show rows with this suggested action."),
    show_all: bool = typer.Option(False, "--all", help="Include unflagged 'approve' rows."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Report-only memory triage: rot flags, staleness claims, merge/amend groups, token cost.

    It never writes. Approve, reject, update and the GUI stay the only paths that change what
    is injected, so the gate stays human.
    """
    from orchestrator.services import memory_triage as mt

    _ensure_ready()
    with _session() as s:
        report = mt.triage(s, status=status, project_slug=project, role=role)
    if as_json:
        _print_json(report)
        return

    rows = report["rows"]
    if action:
        rows = [r for r in rows if r["suggested_action"] == action]
    elif not show_all:
        rows = [r for r in rows if r["suggested_action"] != "approve"]

    table = Table("id", "bucket", "~tok", "action", "flags / grouping / why", title="memory triage")
    for r in rows:
        flags = ", ".join(f["code"] for f in r["flags"]) or "-"
        extra = []
        if r["duplicates_of"]:
            extra.append("same as " + ",".join(f"#{i}" for i in r["duplicates_of"]))
        if r["amend_candidates"]:
            extra.append("amend " + ",".join(f"#{a['id']}" for a in r["amend_candidates"]))
        hard = [f["detail"] for f in r["flags"] if f["code"] != "repo-unavailable"]
        table.add_row(
            str(r["id"]),
            f"{r['category']}/{r['scope']}",
            str(r["tokens"]),
            f"[{_ACTION_STYLE.get(r['suggested_action'], 'white')}]{r['suggested_action']}[/]",
            "\n".join(
                filter(
                    None,
                    [
                        flags,
                        " | ".join(extra) if extra else "",
                        f"[dim]{'; '.join(hard)[:200] or r['reason']}[/dim]",
                    ],
                )
            ),
        )
    console.print(table)

    t = report["totals"]
    counts = "  ".join(f"{a}:{n}" for a, n in t["by_action"].items() if n)
    console.print(
        f"\n[bold]{t['entries']} {report['status']} entries ≈{t['tokens']} tokens[/bold]  {counts}"
    )
    if t["merge_groups"]:
        console.print(
            "duplicate groups: "
            + "  ".join("(" + ",".join(f"#{i}" for i in g) + ")" for g in t["merge_groups"])
        )
    if report["excluded_now"]:
        console.print(
            "[red]approved but NOT injected (over budget):[/red] "
            + ", ".join(
                f"#{x['id']} ({x['bucket']}/{x['scope']} ~{x['tokens']}tok)"
                for x in report["excluded_now"]
            )
        )
    b = report["budgets"]
    for w in report["warnings"]:
        console.print(f"[yellow]warning:[/yellow] {w}")
    if report["budget_enabled"]:
        used = report["current_usage"]
        head = report["headroom"]
        usage = "  ".join(
            f"{k}: {used.get(k, 0)}/{b[k]} tok ({round((head.get(k) or 0) * 100)}%)"
            for k in ("global", "role", "project")
        )
        console.print(f"section caps  {usage}")
    else:
        console.print(
            "memory budget disabled (ORCHESTRATOR_MEMORY_BUDGET_ENABLED=false): "
            "every approved entry injects"
        )
    console.print("\n[bold]if the whole queue were approved:[/bold]")
    for rl, p in report["projection"].items():
        console.print(
            f"  {rl:<11} memory ≈{p['total_now']} → ≈{p['total_after']} tokens"
            + (
                f"   demoted by cap: {', '.join(f'#{i}' for i in p['excluded_after'])}"
                if p["excluded_after"]
                else ""
            )
        )


@memory_app.command("budget")
@handled
def memory_budget(
    project: str = typer.Option(None, help="Project slug to price."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Static cost of injected memory per role, and what is demoted by the caps."""
    from orchestrator.services import memory_triage as mt

    _ensure_ready()
    with _session() as s:
        rep = mt.budget_report(s, project)
    if as_json:
        _print_json(rep)
        return
    table = Table("role", "global", "role", "project", "total", "cap use", "demoted by cap")
    for rl, p in rep["roles"].items():
        sec = p["sections_now"]
        head = p["headroom"]
        table.add_row(
            rl,
            str(sec.get("global", 0)),
            str(sec.get("role", 0)),
            str(sec.get("project", 0)),
            str(p["total_now"]),
            " ".join(
                f"{k[:4]}:{round((head.get(k) or 0) * 100)}%" for k in ("global", "role", "project")
            ),
            ",".join(f"#{i}" for i in p["excluded_now"]) or "-",
        )
    console.print(table)
    for rl, p in rep["roles"].items():
        for w in p["warnings"]:
            console.print(f"[yellow]{rl}: {w}[/yellow]")
    b = rep["budgets"]
    console.print(
        f"caps: global {b['global']}  role {b['role']}  project {b['project']} "
        f"(enabled={rep['budget_enabled']}) | pending {rep['pending_count']} entries "
        f"≈{rep['pending_tokens']} tokens"
    )


@memory_app.command("export")
@handled
def memory_export(
    output: Path = typer.Option(None, "--output", "-o", help="File (default stdout)."),
) -> None:
    from orchestrator.services import memory_service

    _ensure_ready()
    with _session() as s:
        data = memory_service.dumps_export(s)
    if output:
        output.write_text(data)
        console.print(f"exported to {output}")
    else:
        sys.stdout.write(data + "\n")


@memory_app.command("import")
@handled
def memory_import(
    file: Path = typer.Argument(None, help="JSON export file."),
    markdown: bool = typer.Option(
        False, "--markdown", help="Index hand-edited memory/*.md instead."
    ),
) -> None:
    from orchestrator.services import memory_service

    _ensure_ready()
    with _session() as s:
        if markdown:
            n = memory_service.import_markdown_tree(s)
        elif file is None:
            raise ValueError("pass a JSON export file, or --markdown")
        else:
            n = memory_service.import_all(s, json.loads(file.read_text()))
    console.print(f"imported {n} entr{'y' if n == 1 else 'ies'}")


@sync_app.command("status")
@handled
def sync_status() -> None:
    """Show locally tracked, pending, synchronized, and conflicted records."""
    from orchestrator.services import sync_service

    _ensure_ready()
    with _session() as s:
        sync_service.scan_local(s)
        _print_json(sync_service.status(s))


@sync_app.command("now")
@handled
def sync_now() -> None:
    """Pull remote changes, then push pending local changes to Supabase."""
    from orchestrator.services.sync_transport import SupabaseSyncClient

    async def run() -> dict[str, Any]:
        async with SupabaseSyncClient() as client:
            with _session() as s:
                return await client.sync_once(s)

    _ensure_ready()
    _print_json(asyncio.run(run()))


def main() -> None:
    try:
        app()
    finally:
        _flush_notifications_at_exit()


def _flush_notifications_at_exit() -> None:
    """T-18 DoD 3: a CLI process must not leave its pending rows behind.

    The coalescing timer is a thread that dies with the process, so a row created by a CLI
    command would otherwise stay `pending` until an API startup that may never come. Exiting
    delivers what it can and terminally marks what it could not. Inert (and side-effect free)
    when the database has no notifications table yet, so `orchestrator --help` in a fresh home
    still creates nothing.
    """
    try:
        from orchestrator.services import notification_service

        notification_service.flush_pending_at_exit()
    except Exception:  # noqa: BLE001, S110 - a notification must never change the exit code
        pass


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------- agents


@agent_app.command("add")
@handled
def agent_add(
    name: str,
    instructions: str = typer.Option("", help="Standing instructions for every run."),
    description: str = typer.Option(""),
    model: str | None = typer.Option(None, help="Model profile name (default: automatic)."),
    harness: str | None = typer.Option(None, help="Harness adapter (default: routed by model)."),
    project: str | None = typer.Option(None, help="Project (without one it can't edit code)."),
    tag: list[str] = typer.Option([], help="Tags added to every run (repeatable)."),
) -> None:
    from orchestrator.core.schemas import AgentCreate
    from orchestrator.services import agent_service, project_service

    _ensure_ready()
    with _session() as s:
        project_id = project_service.get(s, project).id if project else None
        a = agent_service.create(
            s,
            AgentCreate(
                name=name,
                instructions=instructions,
                description=description,
                model_profile=model,
                harness_adapter=harness,
                project_id=project_id,
                tags=tag,
            ),
        )
        r = agent_service.routing(s, a)
        console.print(f"[green]added[/green] agent #{a.id} {a.name}")
        console.print(f"  model:   {r['profile']}  ({r['profile_reason']})")
        console.print(f"  harness: {r['harness'] or '-'}  ({r['harness_reason']})")


@agent_app.command("list")
@handled
def agent_list() -> None:
    from orchestrator.services import agent_service

    _ensure_ready()
    with _session() as s:
        table = Table("id", "name", "model", "harness", "project", "enabled")
        for a in agent_service.list_agents(s):
            r = agent_service.routing(s, a)
            table.add_row(
                str(a.id),
                a.name,
                r["profile"] or "-",
                r["harness"] or "-",
                str(a.project_id or "-"),
                "yes" if a.enabled else "no",
            )
    console.print(table)


@agent_app.command("run")
@handled
def agent_run(
    agent: str,
    request: str,
    title: str | None = typer.Option(None),
    dod: list[str] = typer.Option([], help="Definition of Done item (repeatable)."),
    no_queue: bool = typer.Option(False, help="Create the task without queueing it."),
) -> None:
    from orchestrator.core.schemas import AgentRun
    from orchestrator.services import agent_service

    _ensure_ready()
    with _session() as s:
        t = agent_service.run(
            s,
            agent_service.get(s, agent),
            AgentRun(request=request, title=title, definition_of_done=dod, queue=not no_queue),
        )
        console.print(f"[green]created[/green] {t.short_id} ({t.status}): {t.title}")
