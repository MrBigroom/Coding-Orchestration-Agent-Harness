"""Project registration and the project doctor."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import Project, Run, Task, utcnow
from orchestrator.core.schemas import ProjectCreate, ProjectUpdate
from orchestrator.services import memory_service
from orchestrator.utils.git import GitError, branch_exists, git_available, is_repo, run_git
from orchestrator.utils.slug import slugify


class ProjectError(ValueError):
    pass


# The contract documents the harness knows about. A phase may change one of these only when its
# own Definition of Done / acceptance criteria / frozen-contracts entry EXPLICITLY GRANT that
# file (docs/build-loop.md, "Unauthorized contract edits"); anything else is harness-detected,
# not model-asserted.
CONTRACT_FILES: tuple[str, ...] = (
    "docs/plan.md",
    "docs/product-spec.md",
    "docs/architecture.md",
    "docs/workflow.md",
    "docs/frozen-contracts.md",
)


def contract_files() -> list[str]:
    """Relative paths of the contract documents the harness watches."""
    return list(CONTRACT_FILES)


def _normalized(path: str) -> str:
    return path.strip().removeprefix("./").removeprefix("/")


def contract_changes(task: Task, changed: list[str]) -> list[str]:
    """Contract files touched by a run (in any commit it produced, recovery commits included)."""
    hits = {_normalized(p) for p in changed}
    return sorted(p for p in CONTRACT_FILES if p in hits)


# A grant names what the phase is allowed to do to the file. Mere mention is not enough: T-8's own
# DoD 8 lists all five contract files while describing the detector, and a mention-based rule would
# have "authorized" exactly the edits it was written to catch (reviewer ruling, T-8 round 5).
_GRANT_RE = re.compile(
    r"""(?xi)
    (?:
        authoriz(?:e|es|ed|ing|ation)|permit(?:s|ted)?|allow(?:s|ed)?|grant(?:s|ed)?|sanction(?:s|ed)?
      | (?:may|can|should)\s+(?:be\s+)?
    )
    \b(?:[^.;\n]{0,40}?)\b
    (?:amend(?:s|ing)?|edit(?:s|ing)?|chang(?:e|es|ing)|updat(?:e|s|ing)|modif(?:y|ies|ying)
      | add(?:s|ing)?|extend(?:s|ing)?|widen(?:s|ing)?|rewrit(?:e|es|ing)|replac(?:e|es|ing)
      | write)
    """
)
# A prohibition naming the file must NEVER read as authorization, even in a sentence that also
# contains a grant verb ("not authorized to change docs/plan.md"). Fails closed.
_PROHIBIT_RE = re.compile(
    r"""(?xi)
    \b(?:do|does|did)\s+not\b | \bdon'?t\b | \bmust\s+not\b | \bmay\s+not\b | \bshall\s+not\b
  | \bcan'?not\b | \bunauthoriz(?:e|ed|es|ation)\b | \bdisallow(?:s|ed)?\b | \bprohibit(?:s|ed)?\b
  | \bforbid(?:s|den)?\b | \bnever\b | \bbanned\b | \bout\s+of\s+scope\b | \bnot\s+permitted\b
  | \bnot\s+allowed\b | \bforbids?\b
  | \bnot\s+(?:be\s+)?(?:authoriz|permit|allow|grant|sanction)\w*
  | \b(?:isn'?t|wasn'?t)\s+(?:authoriz|permit|allow|grant)\w*
    """
)
# Sentence-ish splits for the per-mention test. `\.\s` is safe for paths: the dot in
# "frozen-contracts.md" is followed by a letter, not whitespace.
_CLAUSE_SPLIT = re.compile(r"[\n;]+|\.\s")


def _grant_clauses(texts: list[str]) -> list[str]:
    """Clauses that both grant something and are not prohibitions."""
    out: list[str] = []
    for text in texts:
        for clause in _CLAUSE_SPLIT.split(text or ""):
            if not clause.strip():
                continue
            if _PROHIBIT_RE.search(clause) or not _GRANT_RE.search(clause):
                continue
            out.append(clause.lower())
    return out


def _mentioned(clause: str, path: str) -> bool:
    """Does this clause name the contract file as a path, not as prose?

    A bare basename only counts inside a path-like context (``docs/frozen-contracts.md`` or
    ``frozen-contracts.md``); the full path is always checked first.
    """
    return path.lower() in clause or path.rsplit("/", 1)[-1].lower() in clause


def authorized_contract_files(task: Task) -> set[str]:
    """Contract files this task explicitly authorizes changes to.

    Authorization means a *grant*, not a mention: a Definition of Done item, acceptance criterion
    or frozen-contracts entry must (a) name the file and (b) grant a change to it — "authorized to
    amend docs/frozen-contracts.md to add the ORCH_VERIFY field names". Three things are
    deliberately NOT authorization, each with a test:

    - a **mere mention**, e.g. "intersect the changed-file set with docs/frozen-contracts.md";
    - a **prohibition**, e.g. "Do NOT modify docs/frozen-contracts.md";
    - anything in the task **description**, a commit message or a diffstat — only the three stored
      authorization fields above are read.

    Failing closed is the point: a control that authorizes the edit it was written to detect is
    worse than no control, because it reads as a check that passed.
    """
    texts = [
        *(task.definition_of_done or []),
        *(task.acceptance_criteria or []),
        *(task.frozen_contracts or []),
    ]
    clauses = _grant_clauses(texts)
    if not clauses:
        return set()
    return {p for p in CONTRACT_FILES if any(_mentioned(c, p) for c in clauses)}


def unauthorized_contract_changes(task: Task, changed: list[str]) -> list[str]:
    """Contract files changed by the run that the task does not authorize. Deterministic."""
    allowed = authorized_contract_files(task)
    return [p for p in contract_changes(task, changed) if p not in allowed]


def _touched_paths(cwd: Path, base_branch: str) -> list[str]:
    """Committed diff plus whatever is still uncommitted in the worktree.

    A contract file edited and NOT committed is still an edit; a detector that only looked at
    commits would call that branch contract-clean (T-8 DoD 8).
    """
    from orchestrator.services import git_service

    paths = set(git_service.changed_files(cwd, base_branch))
    split = git_service.status_split(cwd)
    paths.update(split.tracked_modified)
    paths.update(split.untracked_added)
    return sorted(paths)


def contract_findings(
    task: Task, project: Project | None, cwd: Path | None
) -> dict[str, list[str]]:
    """Contract files this run touched, split into authorized vs. unauthorized by the task's
    stored DoD. Empty lists when there is no project worktree to read.

    Lives here because the Supervisor (which must fail the check) and the reviewer payload (which
    must show it) have to agree on the same numbers, computed one way."""
    from orchestrator.services import git_service

    if not cwd or not project or not cwd.is_dir() or not git_service.is_repo(cwd):
        return {"changed": [], "authorized": [], "unauthorized": []}
    hits = contract_changes(task, _touched_paths(cwd, project.base_branch))
    allowed = authorized_contract_files(task)
    return {
        "changed": hits,
        "authorized": sorted(p for p in hits if p in allowed),
        "unauthorized": sorted(p for p in hits if p not in allowed),
    }


def recovery_commits_for(session: Session, task: Task, run: Run | None = None) -> list:
    """Harness-created recovery commits on this task's branch.

    Read-only: the recorded worktree is used when it still exists, otherwise the branch ref is
    read straight from the repository. A question about history must never create a worktree.
    """
    from orchestrator.services import git_service

    project = session.get(Project, task.project_id) if task.project_id else None
    if not project or not task.branch_name:
        return []
    recorded = run.worktree_path if run is not None else None
    if recorded and Path(recorded).is_dir():
        return git_service.recovery_commits(Path(recorded), project.base_branch)
    return git_service.recovery_commits(
        Path(project.path), project.base_branch, ref=task.branch_name
    )


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


def environment_summary(cwd: Path) -> str:
    """Name the world a verification command actually ran in (T-19 DoD 3).

    Recorded inside verification.json's existing `evidence` string because the check-record shape
    and the runs/<run_id>/ layout are frozen. The "tracked-files-only" label is only claimed when
    the cwd really is a linked worktree (`.git` is a file), not for every checkout without a .env.
    """
    cwd = Path(cwd)
    linked_worktree = (cwd / ".git").is_file()
    has_env = (cwd / ".env").is_file()
    has_node_modules = any(
        (cwd / candidate).is_dir() for candidate in ("node_modules", "web/node_modules")
    )
    has_venv = (cwd / ".venv").exists()
    env_state = "present" if has_env else "absent"
    if not has_env and linked_worktree:
        env_state += " (tracked-files-only worktree)"
    return (
        f"cwd={cwd}; local .env {env_state}; "
        f"node_modules {'present' if has_node_modules else 'absent'}; "
        f".venv {'present' if has_venv else 'absent'}"
    )


def doctor(
    project: Project, run_commands: bool = False, env_parity: bool = False
) -> list[dict[str, Any]]:
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
        # Cleanliness is a TRACKED-content verdict (T-8 DoD 9): an untracked local directory
        # (tool state, scratch output) does not block the merge gate and must not fail this
        # check — it is reported separately below so nothing is hidden.
        from orchestrator.services import git_service

        split = git_service.status_split(path)
        add(
            "main checkout clean",
            not split.tracked_dirty,
            (
                "uncommitted changes to tracked files (fine for worktrees; merge gate will "
                "refuse): " + ", ".join(sorted(split.tracked_modified)[:20])
                if split.tracked_dirty
                else ""
            ),
        )
        add(
            "untracked paths in the base checkout",
            True,
            (
                f"{len(split.untracked_added)} untracked path(s), ignored by the merge gate and "
                "never committed by a merge: "
                + ", ".join(sorted(split.untracked_added)[:20])
                if split.untracked_added
                else "none"
            ),
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
    if env_parity:
        checks.extend(run_env_parity_checks(project))
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
ENV_PARITY_SNAPSHOT_KEY = "env-parity"
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


def _marker_problems(rows: list[dict[str, str]], root: Path) -> list[str]:
    """Why an ORCH_VERIFY marker does not prove this worktree was the code under test."""
    problems: list[str] = []
    for row in rows:
        module_file = row.get("module_file", "")
        worktree_root = row.get("worktree_root", "")
        if not module_file or not worktree_root:
            problems.append(f"incomplete marker: {row}")
        elif not _under(module_file, str(root)):
            problems.append(
                f"{row.get('module', '?')} imported from {module_file}, outside the worktree"
            )
        elif not _same_dir(worktree_root, str(root)):
            problems.append(f"wrapper root {worktree_root} is not the worktree {root}")
    return problems


def _drop_snapshots(project: Project, key: str, add, label: str, report_success: bool) -> None:
    """Remove this project's clean proof worktrees for `key` (branch kept)."""
    from orchestrator.services import git_service

    try:
        leftovers = [
            m
            for m in git_service.managed_worktrees()
            if m["project_slug"] == project.slug and m["task_id"] == key
        ]
        for leftover in leftovers:
            note = git_service.remove_worktree(Path(project.path), leftover, dry_run=False)
            if note.startswith("skip"):
                add(label, False, f"{note}; run `orchestrator cleanup`")
            elif report_success:
                add(label, True, note)
    except (OSError, GitError) as e:
        add(label, False, f"{e}; run `orchestrator cleanup`")


def _seeded_env_copy(source: Path, dest: Path) -> Path:
    """Copy a real `.env` 0600, without clobbering one that already exists.

    A plain `shutil.copyfile` creates the destination with the process umask (typically 0644), so
    the operator's credentials would be world-readable under ORCHESTRATOR_HOME. Three properties
    matter here, and each one has been a bug:

    * the bytes are written to a UNIQUE per-invocation staging name and only then linked into
      place, so a crash or a partial `os.write` never leaves a truncated `.env` that a later run
      mistakes for a complete one;
    * the link is create-only (`os.link`), never `os.replace`, so a destination this invocation
      did not create — a tracked `.env` in the snapshot, a retry after a crash, a reused temp
      path — is left byte-for-byte intact instead of being silently overwritten;
    * the staging file is removed on every path, so a failed copy leaves no residue.

    Returns the path this call created. Raises `OSError` (notably `FileExistsError` when `dest`
    already exists, or the source is unreadable); callers turn that into a reported check because
    `doctor` reports and never raises out.
    """
    data = source.read_bytes()
    staging = dest.with_name(f"{dest.name}.{os.getpid()}-{uuid.uuid4().hex}.parity")
    try:
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:  # pragma: no cover - a broken filesystem, not a code path
                    raise OSError(f"short write to {staging}")
                view = view[written:]
        finally:
            os.close(fd)
        os.link(staging, dest)
    finally:
        staging.unlink(missing_ok=True)
    return dest


def run_in_worktree_checks(project: Project) -> list[dict[str, Any]]:
    """Run each configured command in a throwaway worktree of the base branch and report
    whether it exits 0 and whether the import path resolved inside that worktree."""
    from orchestrator.services import git_service, process_service

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
            try:
                code, output, timed_out = process_service.run_simple_sync(
                    argv, info.path, RUN_TIMEOUT_SECONDS
                )
            except Exception as e:  # noqa: BLE001 - doctor reports, it never raises out
                add(f"{label} in worktree", False, f"could not run: {type(e).__name__}: {e}")
                continue
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
            problems = _marker_problems(rows, info.path)
            add(
                f"{label} resolves inside the worktree",
                not problems,
                "; ".join(problems)
                or f"{rows[0].get('module', '?')} -> {rows[0].get('module_file')}",
            )
    finally:
        # Every exit path (green, red, timeout, spawn failure, unexpected error) lands here:
        # a doctor probe must not leave a worktree behind in ORCHESTRATOR_HOME.
        _drop_snapshots(
            project, DOCTOR_SNAPSHOT_KEY, add, "throwaway worktree removed", False
        )
    return checks


# --- opt-in environment-parity probe (`project doctor --env-parity`) -----------------
#
# A DEVELOPER check, never a gate command: run the project's TEST command in a throwaway worktree
# with a copy of the local `.env`, i.e. the world the human actually works in. Off by default and
# CLI-only; it seeds a copy of the env file without reading, logging or committing its contents.

def run_env_parity_checks(project: Project) -> list[dict[str, Any]]:
    from orchestrator.services import git_service, process_service

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    repo = Path(project.path)
    # A worktree left by a killed run is REPORTED, not swept: residue hygiene for the parity path
    # is T-20 and deliberately out of scope for T-19.
    stale = [
        m
        for m in git_service.managed_worktrees()
        if m["project_slug"] == project.slug
        and m["task_id"] == ENV_PARITY_SNAPSHOT_KEY
        and m["exists"]
    ]
    if stale:
        add(
            "env-parity stale worktree",
            False,
            f"{stale[0]['path']} is left over from an interrupted run; run `orchestrator cleanup` "
            "(sweeping it is T-20)",
        )
        return checks

    source_env = repo / ".env"
    if not source_env.is_file():
        add(
            "env-parity suite",
            True,
            f"no .env at {source_env}; nothing to seed (the gate is already env-free)",
        )
        return checks
    if not project.test_command:
        add("env-parity suite", True, "no test command configured")
        return checks

    try:
        info = git_service.ensure_snapshot(
            repo, project.slug, ENV_PARITY_SNAPSHOT_KEY, project.base_branch
        )
    except GitError as e:
        add("env-parity suite", False, f"throwaway worktree failed: {e}")
        return checks
    seeded: Path | None = None
    try:
        try:
            seeded = _seeded_env_copy(source_env, info.path / ".env")
        except OSError as e:
            # `doctor` reports, it never raises out. A tracked `.env` already in
            # the snapshot or an unreadable source is a finding about the parity probe, not a
            # crash; nothing was created by this invocation, so `seeded` stays None and the
            # cleanup below cannot unlink a foreign file.
            add(
                "env-parity .env seeded",
                False,
                f"could not seed a copy of {source_env}: {type(e).__name__}: {e}",
            )
            return checks
        add(
            "env-parity .env seeded",
            True,
            f"copy of {source_env} (contents not logged, mode 0600)",
        )
        try:
            argv = command_argv(project.test_command)
        except ProjectError as e:
            add("env-parity suite", False, str(e))
            return checks
        try:
            code, output, timed_out = process_service.run_simple_sync(
                argv, info.path, RUN_TIMEOUT_SECONDS
            )
        except Exception as e:  # noqa: BLE001 - doctor reports, it never raises out
            add("env-parity suite", False, f"could not run: {type(e).__name__}: {e}")
            return checks
        ok = code == 0 and not timed_out
        detail = f"exit {code} (cwd {info.path}, local .env seeded)"
        if timed_out:
            detail += f" after {RUN_TIMEOUT_SECONDS}s"
        rows = parse_orch_verify(output)
        if not rows:
            ok = False
            detail += (
                "; no ORCH_VERIFY marker: the command is not path-independent and may import "
                "the main checkout — wrap it like scripts/orch_verify.sh"
            )
        else:
            problems = _marker_problems(rows, info.path)
            if problems:
                ok = False
                detail += f"; {'; '.join(problems)}"
        if code != 0 and not timed_out:
            detail += f"; tail: {_tail(output) or 'no output'}"
        add("env-parity suite", ok, detail)
    finally:
        # Remove ONLY the file this invocation created, so the throwaway tree is clean enough for
        # `remove_worktree` to delete it; a killed run's residue is T-20, above.
        if seeded is not None:
            seeded.unlink(missing_ok=True)
        _drop_snapshots(
            project, ENV_PARITY_SNAPSHOT_KEY, add, "env-parity worktree removed", False
        )
    return checks
