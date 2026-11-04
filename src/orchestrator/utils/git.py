"""Low-level git invocation. argv lists only, never shell=True."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass
class GitResult:
    code: int
    stdout: str
    stderr: str


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(
    args: list[str],
    cwd: Path | str,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> GitResult:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    res = GitResult(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return res


def is_repo(path: Path | str) -> bool:
    p = Path(path)
    if not p.is_dir():
        return False
    r = run_git(["rev-parse", "--is-inside-work-tree"], p, check=False)
    return r.code == 0 and r.stdout.strip() == "true"


def branch_exists(repo: Path | str, branch: str) -> bool:
    return (
        run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False
        ).code
        == 0
    )


def head_sha(repo: Path | str) -> str | None:
    r = run_git(["rev-parse", "HEAD"], repo, check=False)
    return (r.stdout.strip() or None) if r.code == 0 else None


def status_porcelain(repo: Path | str, tracked_only: bool = False) -> str:
    """`git status --porcelain -uall`. With tracked_only=True (-uno) untracked paths are NOT listed.

    The default keeps the historical behaviour (untracked paths show up, so "non-empty output"
    has until now meant "dirty"). Callers must pick the flavour that matches the decision they
    are making: a merge/refuse or cleanliness verdict should use tracked_only, while anything
    that must not lose worker output has to look at untracked paths too. `status_entries` gives
    the split directly.

    `-uall` lists untracked *files* rather than collapsing a whole new directory to `dir/`, so
    the paths agree with `git diff --cached --name-status` after staging (recovery commits need
    that comparison to be exact).
    """
    args = ["status", "--porcelain", "-uall"] + (["-uno"] if tracked_only else [])
    return run_git(args, repo).stdout


def status_entries(repo: Path | str) -> list[tuple[str, str]]:
    """Every `git status --porcelain -z -uall` entry as an (XY, path) pair, renames/copy resolved.

    XY is the two-column status: "??" means untracked, anything else is a tracked change
    (modified, deleted, staged, conflicted, renamed). `-z` is used so paths with spaces and
    newlines survive; for R/C entries git emits the original path as the following NUL token.
    """
    raw = run_git(["status", "--porcelain", "-z", "-uall"], repo).stdout
    tokens = [t for t in raw.split("\0") if t]
    out: list[tuple[str, str]] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False  # original path of a rename/copy, already represented
            continue
        xy, path = tok[:2], tok[3:]
        if xy.strip() in ("R", "C"):
            skip_next = True
        if path:
            out.append((xy, path))
    return out


def current_branch(repo: Path | str) -> str | None:
    r = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo, check=False)
    return r.stdout.strip() if r.code == 0 else None
