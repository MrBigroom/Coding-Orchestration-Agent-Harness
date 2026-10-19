"""Git worktree isolation, diff collection, recovery commits, and the merge gate.

Safety rules enforced here:
  - one persistent branch + worktree per task: <worktree_root>/<project_slug>/<task_id>
  - managed worktrees are recorded by a sibling marker (<project_slug>/.managed/<task_id>);
    cleanup only touches worktrees that have a marker AND live under worktree_root
  - never force push, never push at all, never delete branches, never touch the base
    branch except for an explicit, gated `merge --no-ff`
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.config import get_settings
from orchestrator.utils.git import (
    GitError,
    branch_exists,
    current_branch,
    head_sha,
    is_repo,
    run_git,
    status_porcelain,
)
from orchestrator.utils.paths import is_within
from orchestrator.utils.redaction import is_sensitive_path

ORCH_DIR = ".orchestrator"  # per-worktree scratch (handoff file); git-excluded


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    created: bool


def _marker(project_slug: str, task_id: int) -> Path:
    root = get_settings().worktree_root
    assert root
    return root / project_slug / ".managed" / str(task_id)


def worktree_path(project_slug: str, task_id: int) -> Path:
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


def ensure_worktree(
    repo: Path, project_slug: str, task_id: int, branch: str, base_branch: str
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


def is_dirty(path: Path) -> bool:
    return bool(status_porcelain(path).strip())


def recovery_commit(path: Path, label: str) -> str | None:
    """Commit leftover changes in an isolated worktree with a clearly labelled message.
    Sensitive files (.env, keys, credentials) are never staged; they stay untracked."""
    if not is_dirty(path):
        return None
    run_git(["add", "-A"], path)
    staged = run_git(["diff", "--cached", "--name-only", "-z"], path).stdout.split("\0")
    sensitive = [f for f in staged if f and is_sensitive_path(f)]
    if sensitive:
        run_git(["reset", "-q", "--", *sensitive], path)
    if not run_git(["diff", "--cached", "--name-only"], path).stdout.strip():
        return None
    run_git(["commit", "-q", "-m", f"chore(orchestrator-recovery): {label}"], path, env=_git_env())
    return head_sha(path)


def _git_env() -> dict[str, str]:
    import os

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


def merge_command(repo: Path, base_branch: str, branch: str) -> str:
    return f"git -C {repo} checkout {base_branch} && git -C {repo} merge --no-ff {branch}"


def merge_no_ff(repo: Path, base_branch: str, branch: str, message: str) -> str:
    """Merge the accepted phase branch. Refuses unless base is checked out and clean."""
    cur = current_branch(repo)
    if cur != base_branch:
        raise GitError(f"refusing to merge: {repo} has {cur!r} checked out, not {base_branch!r}")
    if is_dirty(repo):
        raise GitError("refusing to merge: base checkout has uncommitted changes")
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
