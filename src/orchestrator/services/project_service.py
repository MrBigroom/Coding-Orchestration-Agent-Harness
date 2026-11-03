"""Project registration and the project doctor."""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import Project, utcnow
from orchestrator.core.schemas import ProjectCreate, ProjectUpdate
from orchestrator.services import memory_service
from orchestrator.utils.git import branch_exists, git_available, is_repo, run_git
from orchestrator.utils.slug import slugify


class ProjectError(ValueError):
    pass


def create(session: Session, data: ProjectCreate) -> Project:
    path = Path(data.path).expanduser().resolve()
    if not path.is_dir():
        raise ProjectError(f"path does not exist: {path}")
    slug = slugify(data.name)
    if session.exec(
        select(Project).where((Project.name == data.name) | (Project.slug == slug))
    ).first():
        raise ProjectError(f"project {data.name!r} already exists")
    base = data.base_branch or _detect_base(path) or get_settings().default_base_branch
    proj = Project(
        name=data.name,
        slug=slug,
        path=str(path),
        base_branch=base,
        test_command=data.test_command,
        lint_command=data.lint_command,
        build_command=data.build_command,
        allowed_commands=data.allowed_commands,
        notes=data.notes,
    )
    session.add(proj)
    session.commit()
    session.refresh(proj)
    memory_service.ensure_layout([slug])
    return proj


def update(session: Session, project: Project, data: ProjectUpdate) -> Project:
    for k, v in data.model_dump(exclude_unset=True).items():
        if k == "path" and v is not None:
            p = Path(v).expanduser().resolve()
            if not p.is_dir():
                raise ProjectError(f"path does not exist: {p}")
            v = str(p)
        setattr(project, k, v)
    project.updated_at = utcnow()
    session.add(project)
    session.commit()
    return project


def get(session: Session, ref: str | int) -> Project:
    proj = None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        proj = session.get(Project, int(ref))
    if proj is None:
        proj = session.exec(
            select(Project).where((Project.name == str(ref)) | (Project.slug == slugify(str(ref))))
        ).first()
    if not proj:
        raise ProjectError(f"project {ref!r} not found")
    return proj


def _detect_base(path: Path) -> str | None:
    if not is_repo(path):
        return None
    r = run_git(["symbolic-ref", "--short", "HEAD"], path, check=False)
    return r.stdout.strip() or None


def command_argv(command: str) -> list[str]:
    """Split a configured command into argv (no shell). Shell syntax is rejected."""
    if any(tok in command for tok in ("&&", "||", "|", ";", "`", "$(", ">", "<")):
        raise ProjectError(
            f"shell syntax is not allowed in commands: {command!r} (wrap it in a script instead)"
        )
    argv = shlex.split(command)
    if not argv:
        raise ProjectError("empty command")
    return argv


def command_allowed(project: Project, command: str) -> bool:
    """Configured test/lint/build commands are always allowed; anything else must match the
    project's allowed_commands (by exact string or by executable name)."""
    configured = {project.test_command, project.lint_command, project.build_command}
    if command in configured:
        return True
    argv0 = command_argv(command)[0]
    return command in project.allowed_commands or argv0 in project.allowed_commands


def doctor(project: Project, run_commands: bool = False) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    path = Path(project.path)
    add("path exists", path.is_dir(), str(path))
    add("git available", git_available())
    repo = path.is_dir() and is_repo(path)
    add("is git repository", repo)
    if repo:
        add(f"base branch '{project.base_branch}' exists", branch_exists(path, project.base_branch))
        dirty = bool(run_git(["status", "--porcelain"], path).stdout.strip())
        add(
            "main checkout clean",
            not dirty,
            "uncommitted changes (fine for worktrees; merge gate will refuse)" if dirty else "",
        )
    for label, cmd in (
        ("test", project.test_command),
        ("lint", project.lint_command),
        ("build", project.build_command),
    ):
        if not cmd:
            add(f"{label} command", True, "not configured (verification will mark N/A)")
            continue
        try:
            argv = command_argv(cmd)
            exe = argv[0]
            found = (path / exe).exists() if "/" in exe else shutil.which(exe) is not None
            add(f"{label} command", found, cmd if found else f"`{exe}` not found")
        except ProjectError as e:
            add(f"{label} command", False, str(e))
    if run_commands:
        checks.extend(run_in_worktree_checks(project))
    return checks


# --- opt-in path-independence probe (`project doctor --run-commands`) ----------------
#
# A gated run executes the project's commands with cwd = the task worktree, and
# `git worktree add` copies tracked files only: no .venv, and an editable install in the
# MAIN checkout's venv silently imports the MAIN checkout. The suite then goes green
# while verifying code that is not the branch under review — a false pass, which is the
# exact failure mode the gate exists to catch. This probe reproduces the condition at
# registration time, in a throwaway worktree of the base branch, and only when asked for
# (--run-commands): doctor never runs project commands unprompted, and the API/GUI route
# does not expose this at all.

DOCTOR_SNAPSHOT_KEY = "doctor"
RUN_TIMEOUT_SECONDS = 900
MARKER_PREFIX = "ORCH_VERIFY|"


def _under(child: str, root: str) -> bool:
    try:
        return Path(child).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def _same_dir(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def parse_orch_verify(output: str) -> list[dict[str, str]]:
    """Parse the `ORCH_VERIFY|mode=…|module=…|module_file=…|worktree_root=…` marker lines
    printed by scripts/orch_verify.sh (see docs/build-loop.md)."""
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith(MARKER_PREFIX):
            continue
        row: dict[str, str] = {}
        for token in line[len(MARKER_PREFIX) :].split("|"):
            key, sep, value = token.partition("=")
            if sep:
                row[key.strip()] = value.strip()
        rows.append(row)
    return rows


def _tail(text: str, limit: int = 400) -> str:
    flat = " / ".join(line for line in text.strip().splitlines() if line.strip())
    return flat[-limit:] if len(flat) > limit else flat


def run_in_worktree_checks(project: Project) -> list[dict[str, Any]]:
    """Run each configured command in a throwaway worktree of the base branch and report
    whether it exits 0 and whether the import path resolved inside that worktree."""
    from orchestrator.services import git_service, process_service
    from orchestrator.utils.git import GitError

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    commands = [
        (label, cmd)
        for label, cmd in (
            ("test", project.test_command),
            ("lint", project.lint_command),
            ("build", project.build_command),
        )
        if cmd
    ]
    if not commands:
        add("commands run in a worktree", True, "no test/lint/build command configured")
        return checks

    repo = Path(project.path)
    try:
        info = git_service.ensure_snapshot(
            repo, project.slug, DOCTOR_SNAPSHOT_KEY, project.base_branch
        )
    except GitError as e:
        add("commands run in a worktree", False, f"throwaway worktree failed: {e}")
        return checks

    add("throwaway worktree created", True, str(info.path))
    try:
        for label, command in commands:
            try:
                argv = command_argv(command)
            except ProjectError as e:
                add(f"{label} in worktree", False, str(e))
                continue
            code, output, timed_out = process_service.run_simple_sync(
                argv, info.path, RUN_TIMEOUT_SECONDS
            )
            ok = code == 0 and not timed_out
            detail = f"exit {code} (cwd {info.path})"
            if timed_out:
                detail += f" after {RUN_TIMEOUT_SECONDS}s"
            if not ok:
                detail += f"; tail: {_tail(output) or 'no output'}"
            add(f"{label} in worktree", ok, detail)
            if label == "build":
                continue  # path independence is only meaningful for the code checks
            rows = parse_orch_verify(output)
            if not rows:
                add(
                    f"{label} resolves inside the worktree",
                    False,
                    "no ORCH_VERIFY marker: this command is not path-independent and may "
                    "import the main checkout — wrap it like scripts/orch_verify.sh",
                )
                continue
            problems: list[str] = []
            for row in rows:
                module_file = row.get("module_file", "")
                worktree_root = row.get("worktree_root", "")
                if not module_file or not worktree_root:
                    problems.append(f"incomplete marker: {row}")
                elif not _under(module_file, str(info.path)):
                    problems.append(
                        f"{row.get('module', '?')} imported from {module_file}, outside the "
                        "worktree (a false pass at the gate)"
                    )
                elif not _same_dir(worktree_root, str(info.path)):
                    problems.append(f"wrapper root {worktree_root} is not the worktree {info.path}")
            add(
                f"{label} resolves inside the worktree",
                not problems,
                "; ".join(problems)
                or f"{rows[0].get('module', '?')} -> {rows[0].get('module_file')}",
            )
    finally:
        leftover = [
            m
            for m in git_service.managed_worktrees()
            if m["project_slug"] == project.slug and m["task_id"] == DOCTOR_SNAPSHOT_KEY
        ]
        for m in leftover:
            note = git_service.remove_worktree(repo, m, dry_run=False)
            if note.startswith("skip"):
                add("throwaway worktree removed", False, f"{note}; run `orchestrator cleanup`")
    return checks
