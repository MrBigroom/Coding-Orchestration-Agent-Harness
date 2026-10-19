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


def run_git(args: list[str], cwd: Path | str, check: bool = True,
            env: dict[str, str] | None = None, timeout: int = 120) -> GitResult:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env, timeout=timeout,
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
    return run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo,
                   check=False).code == 0


def head_sha(repo: Path | str) -> str | None:
    r = run_git(["rev-parse", "HEAD"], repo, check=False)
    return (r.stdout.strip() or None) if r.code == 0 else None


def status_porcelain(repo: Path | str) -> str:
    return run_git(["status", "--porcelain"], repo).stdout


def current_branch(repo: Path | str) -> str | None:
    r = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo, check=False)
    return r.stdout.strip() if r.code == 0 else None
