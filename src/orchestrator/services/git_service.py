"""Git worktree isolation, diff collection, recovery commits, and the merge gate.

Safety rules enforced here:
  - one persistent branch + worktree per task: <worktree_root>/<project_slug>/<task_id>
  - managed worktrees are recorded by a sibling marker (<project_slug>/.managed/<task_id>);
    cleanup only touches worktrees that have a marker AND live under worktree_root
  - never force push, never push at all, never delete branches, never touch the base
    branch except for an explicit, gated `merge --no-ff`
  - worker output is never discarded: a dirty worktree is committed as a labelled,
    trailer-marked recovery commit instead

Two dirty predicates exist because two different decisions are made with them (T-8 DoD 9):
  - `tracked_dirty()` — "did anything that is versioned change?" — gates the merge refusal
    and the doctor cleanliness check, so a local scratch directory cannot block a merge;
  - `is_dirty()` — "is anything at all uncommitted, including untracked files?" — is what
    recovery_commit and cleanup use, because untracked files are worker output too.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.core.config import get_settings
from orchestrator.utils import structured_log
from orchestrator.utils.git import (
    GitError,
    branch_exists,
    current_branch,
    head_sha,
    is_repo,
    run_git,
    status_entries,
    status_porcelain,
)
from orchestrator.utils.paths import is_within
from orchestrator.utils.redaction import is_sensitive_path

ORCH_DIR = ".orchestrator"  # per-worktree scratch (handoff file); git-excluded

# The environment-parity probe (T-20) is the one harness path that deliberately creates a
# throwaway worktree with an env file in it. Its directory name is part of the cleanup contract:
#   * `doctor --env-parity` uses the managed snapshot <worktree_root>/<slug>/env-parity;
#   * scripts/orch_env_parity.sh uses <tmpdir>/orch-env-parity.XXXXXX/tree.
# The parity file is synthetic (variable names only, never the operator's values), but it is still
# residue no worker produced, and a killed run must not leave either the file or the worktree
# registration behind.
PARITY_TASK_ID = "env-parity"
PARITY_TMP_PREFIX = "orch-env-parity."
PARITY_LOCK_NAME = ".lock"
# `<tmpdir>/orch-env-parity.*/.repo` records the checkout the root belongs to, so a project-scoped
# probe ignores another repo's parity run instead of being blocked by it (T-20 review round 2).
PARITY_REPO_MARKER = ".repo"
PARITY_BUSY = "parity run in progress"
_PARITY_ENV_NAMES = (".env",)


class ParityBusy(GitError):
    """Another parity run holds the lock for this destination; never sweep its tree."""


@dataclass(frozen=True)
class ParitySweepAction:
    """One outcome of a parity sweep, with `ok` computed from what was VERIFIED afterwards.

    T-20 review round 3: `ok` must never be derived from the wording of `detail`. A `skip` or a
    `removed parity residue …; skip …: has uncommitted changes` line used to read as success
    because it did not start with "skip", while the tree or its worktree registration survived.
    `ok` is therefore set by the code that checked the filesystem/git afterwards, and it is False
    whenever residue, a registration, or a marker is still present. `busy` distinguishes "a live
    parity run holds this destination" from "the reclaim was attempted and failed".
    """

    detail: str
    ok: bool = True
    busy: bool = False

    def __str__(self) -> str:  # operator-facing text stays exactly what it was
        return self.detail

# Recovery commits keep their historical subject prefix (tests, docs and GUI displays match on
# it) and gain a git trailer so they are machine-identifiable WITHOUT subject string matching.
# Their build-log event keeps the frozen line shape and carries the structured payload inside
# `details` — see utils/structured_log.py.
RECOVERY_SUBJECT_PREFIX = "chore(orchestrator-recovery):"
RECOVERY_TRAILER_KEY = "Orchestrator-Recovery"
UNKNOWN_RUN = "unknown-run"
_RECOVERY_TRAILER = re.compile(rf"(?m)^{re.escape(RECOVERY_TRAILER_KEY)}:\s*(?P<run>.+?)\s*$")


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    created: bool


@dataclass
class DirtyStatus:
    """The two halves of `git status`, split by whether the path was already tracked."""

    tracked_modified: list[str] = field(default_factory=list)
    untracked_added: list[str] = field(default_factory=list)

    @property
    def dirty(self) -> bool:  # anything at all, including untracked scratch
        return bool(self.tracked_modified or self.untracked_added)

    @property
    def tracked_dirty(self) -> bool:  # versioned content changed
        return bool(self.tracked_modified)

    def as_dict(self) -> dict:
        return {
            "tracked_modified": sorted(self.tracked_modified),
            "untracked_added": sorted(self.untracked_added),
        }


@dataclass
class RecoveryCommit:
    """A harness-created commit of leftover worktree output, plus what it swept in."""

    sha: str
    label: str
    run_short_id: str | None
    subject: str
    tracked_modified: list[str]
    untracked_added: list[str]
    skipped_sensitive: list[str]

    @property
    def trailer(self) -> str:
        return f"{RECOVERY_TRAILER_KEY}: {self.run_short_id or UNKNOWN_RUN}"

    def as_dict(self) -> dict:
        return {
            "recovery": True,
            "sha": self.sha,
            "run_short_id": self.run_short_id,
            "trailer": self.trailer,
            "subject": self.subject,
            "label": self.label,
            **DirtyStatus(self.tracked_modified, self.untracked_added).as_dict(),
            "skipped_sensitive": sorted(self.skipped_sensitive),
        }


@dataclass
class CommitInfo:
    """One commit between the base branch and the worktree HEAD, with its file statuses."""

    sha: str
    short: str
    subject: str
    author: str
    recovery: bool
    recovery_run: str | None = None
    files: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sha": self.sha,
            "short": self.short,
            "subject": self.subject,
            "author": self.author,
            "recovery": self.recovery,
            "recovery_run": self.recovery_run,
            "files": self.files,
        }


def _marker(project_slug: str, task_id: int | str) -> Path:
    root = get_settings().worktree_root
    assert root
    return root / project_slug / ".managed" / str(task_id)


def worktree_path(project_slug: str, task_id: int | str) -> Path:
    root = get_settings().worktree_root
    assert root
    return root / project_slug / str(task_id)


def _ensure_excluded(repo: Path) -> None:
    """Add `.orchestrator/` to the repo's local (untracked) info/exclude, idempotently."""
    common = run_git(["rev-parse", "--git-common-dir"], repo).stdout.strip()
    common_path = (repo / common).resolve() if not Path(common).is_absolute() else Path(common)
    exclude = common_path / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if f"/{ORCH_DIR}/" not in existing.splitlines():
        with exclude.open("a") as fh:
            fh.write(
                ("" if existing.endswith("\n") or not existing else "\n")
                + f"# coding-orchestrator scratch\n/{ORCH_DIR}/\n"
            )


def parity_lock_path(project_slug: str) -> Path:
    """Lock handle for the doctor parity destination of one project.

    This is the managed-marker path itself (`.managed/env-parity`), NOT a new directory: the
    frozen layout is `worktrees/<project_slug>/<task_id>` plus the `.managed` markers, so a
    sibling `.locks/` tree would invent a path scheme the contract does not grant (T-20 review
    round 3). Holding an exclusive `flock` on the marker changes what the marker RECORDS not at
    all — `ensure_snapshot` still writes `<path>\n(detached)\n` to it — and it means the lock
    disappears together with the marker, so no lock file of its own can be left behind.
    """
    return _marker(project_slug, PARITY_TASK_ID)


@contextlib.contextmanager
def parity_lock(project_slug: str) -> Iterator[None]:
    """Hold the doctor parity lock for the lifetime of a parity run.

    An interrupted run releases the lock with the process, so a later sweep can tell an orphaned
    tree (lock free) from one another process is still using (lock busy). The lock path lives
    beside the managed marker, never inside the throwaway worktree, because the whole worktree is
    deleted by cleanup.
    """
    import fcntl  # POSIX-only, imported here so the module itself stays platform-portable

    path = parity_lock_path(project_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ParityBusy(f"another parity run holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def _parity_lock_at(path: Path) -> Iterator[bool]:
    """Non-blocking lock used by the sweeper: yields False (and touches nothing) when busy."""
    import fcntl  # POSIX-only, imported here so the module itself stays platform-portable

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def is_parity_worktree(path: Path | str) -> bool:
    """True for the two parity destinations the harness creates (never a task worktree)."""
    p = Path(path)
    if p.name == PARITY_TASK_ID and (p.parent / ".managed" / PARITY_TASK_ID).exists():
        return True
    return p.name == "tree" and p.parent.name.startswith(PARITY_TMP_PREFIX)


def _is_parity_residue_name(name: str) -> bool:
    return name in _PARITY_ENV_NAMES or (
        name.startswith(".env.") and name.endswith(".parity")
    )


def _parity_residue_paths(path: Path) -> list[Path]:
    """Untracked synthetic parity files in a parity worktree (never a versioned/tracked one)."""
    if path.is_symlink() or not path.is_dir() or not is_parity_worktree(path):
        return []
    found: list[Path] = []
    candidates = [path / name for name in _PARITY_ENV_NAMES]
    candidates.extend(path.glob(".env.*.parity"))
    for candidate in candidates:
        if not (candidate.is_file() or candidate.is_symlink()):
            continue
        tracked = run_git(
            ["ls-files", "--error-unmatch", "--", candidate.name], path, check=False
        ).code == 0
        if not tracked:
            found.append(candidate)
    return found


def _remove_parity_residue(path: Path) -> list[str]:
    """Delete untracked parity files from a parity worktree; never a versioned/tracked file.

    `.env` is gitignored in this repository, so `git status` does not even list it; the residue
    check is filesystem-based on purpose. Returns the removed paths.
    """
    removed: list[str] = []
    for candidate in _parity_residue_paths(path):
        candidate.unlink(missing_ok=True)
        removed.append(str(candidate))
    return removed


def _repo_of_worktree(path: Path) -> Path | None:
    """The main checkout owning a linked worktree, or None when the metadata is gone."""
    if path.is_symlink() or not path.is_dir():
        return None
    common = run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], path, check=False
    )
    if common.code != 0 or not common.stdout.strip():
        return None
    common_path = Path(common.stdout.strip())
    return common_path.parent if common_path.name == ".git" else None


def _registered_in(tree: Path) -> Path | None:
    """The repo that REGISTERS `tree` as a LINKED worktree, or None.

    `git worktree remove` is a mutation on someone's git metadata, so it only runs for a path git
    lists as a linked worktree — never for the repo's own main checkout, which a `tree/` that is
    an unrelated clone would be. An unregistered directory is deleted with rmtree alone
    (T-20 review 2).
    """
    repo = _repo_of_worktree(tree)
    if repo is None:
        return None
    listed = run_git(["worktree", "list", "--porcelain"], repo, check=False)
    if listed.code != 0:
        return None
    target = os.path.realpath(tree)
    main = os.path.realpath(repo)
    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        entry = os.path.realpath(line[len("worktree ") :].strip())
        if entry == target and entry != main:
            return repo
    return None


def _listed_worktree(repo: Path, tree: Path) -> bool:
    """Is `tree` still listed by git as a worktree of `repo`?

    Used to verify a removal CLAIM after the directory is gone (T-20 review round 3):
    `git worktree remove` and `shutil.rmtree` both run with errors suppressed, so the only honest
    evidence that the registration is gone is `git worktree list` afterwards. When the listing
    cannot be read the answer is True (fail closed: never claim a registration was removed).
    """
    listed = run_git(["worktree", "list", "--porcelain"], repo, check=False)
    if listed.code != 0:
        return True
    target = os.path.realpath(tree)
    return any(
        os.path.realpath(line[len("worktree ") :].strip()) == target
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    )


def _sweep_managed_parity(project_slug: str | None, dry_run: bool) -> list[ParitySweepAction]:
    actions: list[ParitySweepAction] = []
    try:
        infos = managed_worktrees()
    except OSError as exc:  # an unreadable ORCHESTRATOR_HOME is reported, never raised
        return [
            ParitySweepAction(f"skip managed parity sweep: {type(exc).__name__}: {exc}", ok=False)
        ]
    for info in infos:
        if info["task_id"] != PARITY_TASK_ID:
            continue
        if project_slug and info["project_slug"] != project_slug:
            continue
        path: Path = info["path"]
        try:
            with _parity_lock_at(parity_lock_path(info["project_slug"])) as acquired:
                if not acquired:
                    actions.append(
                        ParitySweepAction(
                            f"skip {path}: {PARITY_BUSY}", ok=False, busy=True
                        )
                    )
                    continue
                actions.extend(_sweep_one_managed_parity(info, dry_run))
        except OSError as exc:
            actions.append(
                ParitySweepAction(f"skip {path}: {type(exc).__name__}: {exc}", ok=False)
            )
    return actions


def _sweep_one_managed_parity(info: dict, dry_run: bool) -> list[ParitySweepAction]:
    """Reclaim one managed `env-parity` snapshot, holding its lock.

    The action's `ok` is decided by re-checking the tree and the marker afterwards, so a
    `remove_worktree` that refused the tree ("has uncommitted changes") is reported as a failure
    rather than being folded behind a successful-looking "removed parity residue" prefix.
    """
    path: Path = info["path"]
    marker: Path = info["marker"]
    if not path.exists():
        if dry_run:
            return [ParitySweepAction(f"would remove stale parity marker {marker}")]
        marker.unlink(missing_ok=True)
        gone = not marker.exists()
        return [
            ParitySweepAction(
                f"removed stale parity marker {marker}"
                if gone
                else f"could not remove stale parity marker {marker}",
                ok=gone,
            )
        ]
    repo = _repo_of_worktree(path)
    residue = _parity_residue_paths(path)
    if dry_run:
        detail = (
            "would remove parity residue " + ", ".join(str(p) for p in residue) + "; "
            if residue
            else ""
        )
        return [ParitySweepAction(f"{detail}{remove_worktree(repo, info, dry_run=True)}")]
    removed = _remove_parity_residue(path)
    note = remove_worktree(repo, info, dry_run=False)
    residue_note = f"removed parity residue {', '.join(removed)}; " if removed else ""
    if path.exists() or marker.exists():
        after = f" and {len(removed)} residue file(s)" if removed else ""
        return [
            ParitySweepAction(
                f"could not remove parity worktree {path}{after}: {note}", ok=False
            )
        ]
    return [ParitySweepAction(f"{residue_note}{note}")]


def _parity_root_owner(root: Path) -> Path | None:
    """The checkout recorded in `<root>/.repo`, or None when the marker is missing/unreadable."""
    marker = root / PARITY_REPO_MARKER
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        first = marker.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return None
    if not first:
        return None
    try:
        return Path(first[0]).resolve()
    except (OSError, RuntimeError):
        return None


def _untrusted_parity_root(root: Path) -> str | None:
    """Why `root` must be left alone, or None when it is a root this harness created.

    A name match is not authority to delete (T-20 review round 2). `glob` yields whatever sits
    in a shared /tmp, so the root must be a REAL directory (lstat, never followed: a symlink
    named `orch-env-parity.x` would otherwise let a sweep delete `root/tree` anywhere on disk),
    owned by this uid, and not group/world writable by someone else.
    """
    try:
        st = root.lstat()
    except OSError as exc:
        return f"skip {root}: could not inspect: {type(exc).__name__}: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return f"skip {root}: symlink, not a parity root this harness created"
    if not stat.S_ISDIR(st.st_mode):
        return f"skip {root}: not a directory"
    if st.st_uid != os.getuid():
        return f"skip {root}: owned by uid {st.st_uid}, not {os.getuid()}"
    if st.st_mode & 0o022:
        return f"skip {root}: group/world writable (mode {oct(st.st_mode & 0o777)})"
    return None


def _sweep_temp_parity(temp_root: Path, dry_run: bool) -> list[ParitySweepAction]:
    """Reclaim one <tmpdir>/orch-env-parity.* root created by scripts/orch_env_parity.sh.

    Every removal is verified afterwards: `worktree remove` runs with `check=False`, rmtree with
    `ignore_errors=True`, so "removed ..." is claimed only when the tree, the git registration
    and the root are all gone (T-20 review round 3).
    """
    actions: list[ParitySweepAction] = []
    lock = temp_root / PARITY_LOCK_NAME
    try:
        with _parity_lock_at(lock) as acquired:
            if not acquired:
                return [
                    ParitySweepAction(
                        f"skip {temp_root}: {PARITY_BUSY}", ok=False, busy=True
                    )
                ]
            tree = temp_root / "tree"
            tree_link = tree.is_symlink()
            has_tree = not tree_link and tree.exists()
            if dry_run:
                if tree_link:
                    actions.append(
                        ParitySweepAction(f"would remove orphaned parity symlink {tree}")
                    )
                elif has_tree:
                    actions.append(
                        ParitySweepAction(f"would remove orphaned parity tree {tree}")
                    )
                actions.append(ParitySweepAction(f"would remove orphaned parity root {temp_root}"))
                return actions
            if tree_link:
                # Unlink the LINK only: never rmtree through it.
                tree.unlink(missing_ok=True)
                gone = not tree.is_symlink()
                actions.append(
                    ParitySweepAction(
                        f"removed orphaned parity symlink {tree}"
                        if gone
                        else f"could not remove orphaned parity symlink {tree}",
                        ok=gone,
                    )
                )
            elif has_tree:
                repo = _registered_in(tree)
                if repo is not None:
                    run_git(["worktree", "remove", "--force", str(tree)], repo, check=False)
                    run_git(["worktree", "prune"], repo, check=False)
                if tree.exists():
                    shutil.rmtree(tree, ignore_errors=True)
                clean = not tree.exists() and not (
                    repo is not None and _listed_worktree(repo, tree)
                )
                actions.append(
                    ParitySweepAction(
                        f"removed orphaned parity tree {tree}"
                        if clean
                        else f"could not remove orphaned parity tree {tree}",
                        ok=clean,
                    )
                )
            # The lock is inside the root and this process holds it open; POSIX unlink is safe.
            shutil.rmtree(temp_root, ignore_errors=True)
            gone = not temp_root.exists()
            actions.append(
                ParitySweepAction(
                    f"removed orphaned parity root {temp_root}"
                    if gone
                    else f"could not remove orphaned parity root {temp_root}",
                    ok=gone,
                )
            )
    except OSError as exc:
        # A shared /tmp may hold a root this process cannot open. `doctor` reports, never raises.
        return [ParitySweepAction(f"skip {temp_root}: {type(exc).__name__}: {exc}", ok=False)]
    return actions


def sweep_parity_residue(
    project_slug: str | None = None,
    repo: Path | str | None = None,
    dry_run: bool = False,
) -> list[ParitySweepAction]:
    """Reclaim orphaned parity trees and their worktree registrations.

    Called at the start of `orchestrator cleanup` and of the doctor parity probe (T-20). A tree
    whose parity lock is held by a live process is skipped and reported, never swept.

    `project_slug` narrows the managed `env-parity` snapshot to one project. `repo` narrows the
    shell-script roots under the shared tempdir to the checkout recorded in their `.repo` marker,
    because another repo's live parity run is not this project's check to fail on. With neither,
    every root this uid owns is considered (`orchestrator cleanup`). `dry_run` only reports.

    Each result carries its own verified `ok`, so a caller reports residue as unreclaimed when a
    tree, registration or marker is still there instead of parsing the wording of `detail`.
    """
    actions: list[ParitySweepAction] = []
    try:
        roots = sorted(Path(tempfile.gettempdir()).glob(f"{PARITY_TMP_PREFIX}*"))
    except OSError as exc:
        roots = []
        actions.append(
            ParitySweepAction(f"skip tempdir sweep: {type(exc).__name__}: {exc}", ok=False)
        )
    wanted = Path(repo).resolve() if repo is not None else None
    for root in roots:
        reason = _untrusted_parity_root(root)
        if reason is not None:
            # Only surface a refusal for a root this project would otherwise have considered.
            if wanted is None or _parity_root_owner(root) == wanted:
                actions.append(ParitySweepAction(reason, ok=False))
            continue
        owner = _parity_root_owner(root)
        if wanted is not None and owner != wanted:
            # Another checkout's tree (or an unmarked pre-T-20 root): not ours to judge here.
            continue
        actions.extend(_sweep_temp_parity(root, dry_run))
    actions.extend(_sweep_managed_parity(project_slug, dry_run))
    return actions


def ensure_snapshot(
    repo: Path, project_slug: str, key: int | str, base_branch: str
) -> WorktreeInfo:
    """A throwaway worktree of the base branch on a detached HEAD, so it leaves no branch."""
    if not is_repo(repo):
        raise GitError(f"{repo} is not a git repository")
    if not branch_exists(repo, base_branch):
        raise GitError(f"base branch {base_branch!r} does not exist in {repo}")
    path = worktree_path(project_slug, key)
    marker = _marker(project_slug, key)
    _ensure_excluded(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "prune"], repo, check=False)
    run_git(["worktree", "add", "--detach", str(path), base_branch], repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{path}\n(detached)\n")
    return WorktreeInfo(path, "", created=True)


def ensure_worktree(
    repo: Path, project_slug: str, task_id: int | str, branch: str, base_branch: str
) -> WorktreeInfo:
    if not is_repo(repo):
        raise GitError(f"{repo} is not a git repository")
    path = worktree_path(project_slug, task_id)
    marker = _marker(project_slug, task_id)
    _ensure_excluded(repo)
    if path.exists():
        if not marker.exists():
            raise GitError(f"{path} exists but is not managed by the orchestrator; refusing")
        if not is_repo(path):
            raise GitError(f"{path} is managed but no longer a git worktree; run cleanup")
        cur = current_branch(path)
        if cur != branch:
            raise GitError(f"worktree {path} is on {cur!r}, expected {branch!r}")
        return WorktreeInfo(path, branch, created=False)

    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "prune"], repo, check=False)
    if branch_exists(repo, branch):
        run_git(["worktree", "add", str(path), branch], repo)
    else:
        if not branch_exists(repo, base_branch):
            raise GitError(f"base branch {base_branch!r} does not exist in {repo}")
        run_git(["worktree", "add", "-b", branch, str(path), base_branch], repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{path}\n{branch}\n")
    (path / ORCH_DIR).mkdir(exist_ok=True)
    return WorktreeInfo(path, branch, created=True)


def status_split(path: Path) -> DirtyStatus:
    """Split the uncommitted state into tracked changes vs. untracked new files.

    Untracked synthetic parity files (`.env`, `.env.*.parity`) in a parity worktree are NOT worker
    dirt: the harness created them and cleanup removes them (T-20 DoD 3). They are filtered here,
    so recovery_commit never stages one and remove_worktree does not refuse the tree. A TRACKED
    `.env` still counts: only paths the harness added are exempt.
    """
    parity = is_parity_worktree(path)
    tracked: list[str] = []
    untracked: list[str] = []
    for xy, p in status_entries(path):
        if parity and xy == "??" and _is_parity_residue_name(Path(p).name):
            continue
        (untracked if xy == "??" else tracked).append(p)
    return DirtyStatus(tracked_modified=tracked, untracked_added=untracked)


def is_dirty(path: Path) -> bool:
    """Anything uncommitted at all, INCLUDING untracked paths (scratch files, local tool
    directories). Use it only where losing an untracked file would be the damage —
    recovery_commit and cleanup. Merge/refuse and cleanliness verdicts want `tracked_dirty`.

    Parity residue is excluded by `status_split` for parity destinations only; a task worktree
    keeps the inclusive predicate, so worker output is still never discarded."""
    return status_split(path).dirty


def tracked_dirty(path: Path) -> bool:
    """Versioned content is uncommitted (`git status --porcelain -uno`). This is the predicate a
    merge refusal or a "is this checkout clean?" verdict must key on: an untracked local
    directory is not an uncommitted change to anything versioned."""
    return bool(status_porcelain(path, tracked_only=True).strip())


MAX_PATHS_IN_MESSAGE = 40


def _join_paths(paths: list[str]) -> str:
    ordered = sorted(paths)
    if not ordered:
        return "none"
    shown = ordered[:MAX_PATHS_IN_MESSAGE]
    suffix = f" (+{len(ordered) - len(shown)} more)" if len(ordered) > len(shown) else ""
    return ", ".join(shown) + suffix


def recovery_message(info: RecoveryCommit) -> str:
    """The subject stays byte-compatible with the historical prefix; the body labels the
    tracked/untracked split and the final paragraph carries the machine-readable trailer."""
    lines = [
        f"{RECOVERY_SUBJECT_PREFIX} {info.label}",
        "",
        "Created by coding-orchestrator so uncommitted worktree output would not be lost.",
        "This content was NEVER self-checked by the Worker: re-verify with the project's lint",
        "and test commands before trusting it, and do not read it as authored work.",
        "",
        f"Tracked modifications ({len(info.tracked_modified)}): "
        + _join_paths(info.tracked_modified),
        f"Newly added untracked paths ({len(info.untracked_added)}): "
        + _join_paths(info.untracked_added),
    ]
    if info.skipped_sensitive:
        lines.append(
            f"Skipped as sensitive ({len(info.skipped_sensitive)}): "
            + _join_paths(info.skipped_sensitive)
        )
    lines += ["", info.trailer]
    return "\n".join(lines) + "\n"


def _staged_name_status(path: Path) -> list[tuple[str, str]]:
    """(status, path) for everything staged right now, NUL-safe (`--name-status -z`)."""
    raw = run_git(["diff", "--cached", "--name-status", "-z"], path).stdout
    tokens = [t for t in raw.split("\0") if t]
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if i >= len(tokens):
            break
        p = tokens[i]
        i += 1
        if status[:1] in ("R", "C"):  # rename/copy carries the original path as the next token
            if i < len(tokens):
                i += 1
        out.append((status, p))
    return out


def recovery_commit_info(
    path: Path, label: str, run_short_id: str | None = None
) -> RecoveryCommit | None:
    """Commit leftover changes in an isolated worktree, labelled AND trailer-marked as harness
    wreckage. Returns None when there is nothing committable.

    Deliberately uses the inclusive predicate: untracked worker output is committed (never
    discarded) but reported separately from tracked modifications, so a reviewer can tell real
    edits from scratch files instead of guessing from one flat commit. Sensitive files (.env,
    keys, credentials) are never staged; they stay untracked. In a PARITY worktree the synthetic
    env is removed before staging at all: it is harness residue, not worker output (T-20 DoD 3)."""
    parity = is_parity_worktree(path)
    if parity:
        _remove_parity_residue(path)
    split = status_split(path)
    if not split.dirty:
        return None
    run_git(["add", "-A"], path)
    staged = run_git(["diff", "--cached", "--name-only", "-z"], path).stdout.split("\0")
    # Two independent nets: `is_sensitive_path` keys on the file shape, the parity name check keys
    # on the destination, so neither alone decides whether residue can reach a commit.
    sensitive = [
        f
        for f in staged
        if f
        and (
            is_sensitive_path(f)
            or (parity and _is_parity_residue_name(Path(f).name))
        )
    ]
    if sensitive:
        run_git(["reset", "-q", "--", *sensitive], path)
    staged_now = _staged_name_status(path)
    if not staged_now:
        return None
    # Provenance decides the label: a path that was UNTRACKED when the harness captured it is
    # scratch output, everything else was already versioned (real edits to tracked content).
    was_untracked = set(split.untracked_added)
    info = RecoveryCommit(
        sha="",
        label=label,
        run_short_id=run_short_id,
        subject=f"{RECOVERY_SUBJECT_PREFIX} {label}",
        tracked_modified=[p for _, p in staged_now if p not in was_untracked],
        untracked_added=[p for _, p in staged_now if p in was_untracked],
        skipped_sensitive=sensitive,
    )
    run_git(["commit", "-q", "-m", recovery_message(info)], path, env=_git_env())
    info.sha = head_sha(path) or ""
    return info if info.sha else None


def recovery_commit(path: Path, label: str, run_short_id: str | None = None) -> str | None:
    """Back-compatible wrapper: the recovery commit SHA, or None when nothing landed."""
    info = recovery_commit_info(path, label, run_short_id)
    return info.sha if info else None


def recovery_event_details(summary: str, info: RecoveryCommit) -> str:
    """`details` for the existing recovery_commit build-log event: the human summary plus the
    structured recovery payload (tracked vs. untracked paths, trailer, SHA)."""
    return structured_log.attach(summary, info.as_dict())


def recovery_run_of(commit_message: str) -> str | None:
    """The run id from an `Orchestrator-Recovery:` trailer in a commit message, or None."""
    m = _RECOVERY_TRAILER.search(commit_message or "")
    return (m.group("run").strip() or UNKNOWN_RUN) if m else None


def parse_recovery_event_details(details: str) -> dict | None:
    """Reader for `recovery_event_details`; None for a non-recovery or legacy line."""
    payload = structured_log.parse(details)
    if payload is None or not payload.get("recovery"):
        return None
    return payload


def _git_env() -> dict[str, str]:
    s = get_settings()
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": s.git_user_name,
        "GIT_AUTHOR_EMAIL": s.git_user_email,
        "GIT_COMMITTER_NAME": s.git_user_name,
        "GIT_COMMITTER_EMAIL": s.git_user_email,
    }


def diff_against_base(path: Path, base_branch: str) -> tuple[str, str]:
    """Return (patch, diffstat) of the phase branch vs. its merge-base with base_branch."""
    patch = run_git(["diff", f"{base_branch}...HEAD"], path).stdout
    stat = run_git(["diff", "--stat", f"{base_branch}...HEAD"], path).stdout
    return patch, stat


def changed_files(path: Path, base_branch: str) -> list[str]:
    out = run_git(["diff", "--name-only", f"{base_branch}...HEAD"], path).stdout
    return [line for line in out.splitlines() if line]


_LOG_FIELDS = "%x1e%H%x1f%h%x1f%s%x1f%an <%ae>"


def commits_against_base(
    path: Path, base_branch: str, ref: str = "HEAD"
) -> list[CommitInfo]:
    """Every commit between the base branch and `ref` (default: this worktree's HEAD),
    oldest first.

    Recovery commits are identified by their `Orchestrator-Recovery:` trailer, never by string
    matching on the subject, so a Worker that copies the subject wording cannot hide wreckage
    (and a hand-authored commit cannot impersonate a recovery commit)."""
    spec = f"{base_branch}..{ref}"
    try:
        meta = run_git(
            [
                "log",
                "--reverse",
                "--name-status",
                f"--format={_LOG_FIELDS}",
                spec,
            ],
            path,
            check=False,
        )
        bodies = run_git(["log", "--reverse", "--format=%x1e%H%x1f%B", spec], path, check=False)
    except GitError:  # base branch unknown, empty history, not a repo: report no commits
        return []
    if meta.code != 0:
        return []

    runs: dict[str, str] = {}
    for chunk in bodies.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        sha, _, body = chunk.partition("\x1f")
        runs[sha.strip()] = body

    commits: list[CommitInfo] = []
    for chunk in meta.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        lines = [line for line in chunk.splitlines() if line]
        if not lines:
            continue
        header = lines[0].lstrip("\x1e")
        fields = header.split("\x1f")
        if len(fields) < 4:
            continue
        sha, short, subject, author = fields[0], fields[1], fields[2], fields[3]
        body = runs.get(sha, "")
        m = _RECOVERY_TRAILER.search(body)
        files = []
        for line in lines[1:]:
            status, _, fname = line.partition("\t")
            if not fname:
                continue
            files.append({"status": status.strip(), "path": fname.strip()})
        commits.append(
            CommitInfo(
                sha=sha,
                short=short,
                subject=subject,
                author=author,
                recovery=m is not None,
                recovery_run=(m.group("run").strip() or UNKNOWN_RUN) if m else None,
                files=files,
            )
        )
    return commits


def recovery_commits(path: Path, base_branch: str, ref: str = "HEAD") -> list[CommitInfo]:
    """The harness-created commits on this branch (trailer-detected, not subject-matched)."""
    return [c for c in commits_against_base(path, base_branch, ref) if c.recovery]


def format_recovery_commit_list(commits: list[CommitInfo]) -> str:
    """Human-readable listing used in prompts and the reviewer payload."""
    out = []
    for c in commits:
        added = [f["path"] for f in c.files if f["status"].startswith("A")]
        edited = [f["path"] for f in c.files if not f["status"].startswith("A")]
        out.append(
            f"- {c.short} (recovery of run {c.recovery_run or UNKNOWN_RUN}): {c.subject}\n"
            f"  tracked modifications: {_join_paths(edited)}\n"
            f"  newly added (untracked before the commit): {_join_paths(added)}"
        )
    return "\n".join(out)


def merge_command(repo: Path, base_branch: str, branch: str) -> str:
    return f"git -C {repo} checkout {base_branch} && git -C {repo} merge --no-ff {branch}"


def merge_no_ff(repo: Path, base_branch: str, branch: str, message: str) -> str:
    """Merge the accepted phase branch. Refuses unless base is checked out and clean.

    "Clean" here means TRACKED content uncommitted (`-uno`): an untracked local directory (a
    tool folder, scratch output) is not an uncommitted change to anything versioned and must
    not refuse a legitimate merge — it can never be lost by a fast-forward/no-ff merge of a
    different branch."""
    cur = current_branch(repo)
    if cur != base_branch:
        raise GitError(f"refusing to merge: {repo} has {cur!r} checked out, not {base_branch!r}")
    if tracked_dirty(repo):
        split = status_split(repo)
        detail = " to tracked files: " + _join_paths(split.tracked_modified)
        if split.untracked_added:
            detail += ", while " + _join_paths(split.untracked_added) + " stay untracked"
        raise GitError("refusing to merge: base checkout has uncommitted changes" + detail)
    run_git(["merge", "--no-ff", "-m", message, branch], repo, env=_git_env())
    sha = head_sha(repo)
    assert sha
    return sha


def managed_worktrees() -> list[dict]:
    root = get_settings().worktree_root
    assert root
    out = []
    if not root.exists():
        return out
    for marker_dir in root.glob("*/.managed"):
        slug = marker_dir.parent.name
        for marker in marker_dir.iterdir():
            path = root / slug / marker.name
            out.append(
                {
                    "project_slug": slug,
                    "task_id": marker.name,
                    "path": path,
                    "exists": path.exists(),
                    "marker": marker,
                }
            )
    return out


def remove_worktree(repo: Path | None, info: dict, dry_run: bool = True) -> str:
    """Remove one managed worktree (never the branch). Returns a description of the action."""
    root = get_settings().worktree_root
    assert root
    path: Path = info["path"]
    marker: Path = info["marker"]
    if not marker.exists() or not is_within(root, path):
        return f"skip {path}: not managed"
    # Parity files are harness residue, not worker output: remove them explicitly before the
    # dirty check so a leaked .env can never make cleanup refuse the tree (T-20 DoD 3). A
    # dry-run must not delete, and `status_split` already exempts parity residue from the
    # dirty predicate.
    if not dry_run:
        _remove_parity_residue(path)
    if path.exists() and is_repo(path) and is_dirty(path):
        return f"skip {path}: has uncommitted changes"
    if dry_run:
        return f"would remove worktree {path} (branch kept)"
    if path.exists():
        if repo and is_repo(repo):
            run_git(["worktree", "remove", str(path)], repo, check=False)
        if path.exists():
            # Directory left behind (repo moved or worktree metadata gone): remove our files.
            import shutil

            shutil.rmtree(path)
    marker.unlink(missing_ok=True)
    if repo and is_repo(repo):
        run_git(["worktree", "prune"], repo, check=False)
    return f"removed worktree {path} (branch kept)"
