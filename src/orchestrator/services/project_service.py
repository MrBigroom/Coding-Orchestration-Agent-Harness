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
    if session.exec(select(Project).where((Project.name == data.name) | (Project.slug == slug))
                    ).first():
        raise ProjectError(f"project {data.name!r} already exists")
    base = data.base_branch or _detect_base(path) or get_settings().default_base_branch
    proj = Project(name=data.name, slug=slug, path=str(path), base_branch=base,
                   test_command=data.test_command, lint_command=data.lint_command,
                   build_command=data.build_command, allowed_commands=data.allowed_commands,
                   notes=data.notes)
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
        proj = session.exec(select(Project).where(
            (Project.name == str(ref)) | (Project.slug == slugify(str(ref))))).first()
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
        raise ProjectError(f"shell syntax is not allowed in commands: {command!r} "
                           "(wrap it in a script instead)")
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


def doctor(project: Project) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    path = Path(project.path)
    add("path exists", path.is_dir(), str(path))
    add("git available", git_available())
    repo = path.is_dir() and is_repo(path)
    add("is git repository", repo)
    if repo:
        add(f"base branch '{project.base_branch}' exists",
            branch_exists(path, project.base_branch))
        dirty = bool(run_git(["status", "--porcelain"], path).stdout.strip())
        add("main checkout clean", not dirty,
            "uncommitted changes (fine for worktrees; merge gate will refuse)" if dirty else "")
    for label, cmd in (("test", project.test_command), ("lint", project.lint_command),
                       ("build", project.build_command)):
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
    return checks
