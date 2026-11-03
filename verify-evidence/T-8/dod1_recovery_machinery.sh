#!/usr/bin/env bash
# T-8 DoD 1-5/9 evidence, run against real git objects:
#   (a) this branch's own history through the shipped detector,
#   (b) a fresh worktree with half-finished edits swept into a recovery commit (the run-5 shape),
#   (c) the untracked-only worktree that must be merge-clean but still swept (DoD 9).
# Offline: temp git repos under /tmp, no network, no services, nothing installed.
set -uo pipefail
WT=/home/mrbigroom/.local/share/coding-orchestrator/worktrees/coding-orchestrator/8
PY=/run/media/mrbigroom/Backup2TB/Projects/Coding-Orchestration-Agent-Harness/.venv/bin/python
cd "$WT" || exit 1

echo "### (a) this branch's history, read by git_service.commits_against_base / recovery_commits"
PYTHONPATH="$WT/src" "$PY" - <<'PY'
from pathlib import Path

from orchestrator.services import git_service

print("  RECOVERY_SUBJECT_PREFIX =", repr(git_service.RECOVERY_SUBJECT_PREFIX))
print("  RECOVERY_TRAILER_KEY    =", repr(git_service.RECOVERY_TRAILER_KEY))
commits = git_service.commits_against_base(Path("."), "main")
print(f"  main..HEAD: {len(commits)} commit(s)")
for c in commits:
    print(f"    {c.short} recovery={c.recovery!s:<5} run={c.recovery_run} {c.subject[:64]}")
print("  recovery_commits() ->", [c.short for c in git_service.recovery_commits(Path('.'), "main")])
PY

echo
echo "### (b) a fresh recovery commit: prefix kept, trailer added, tracked/untracked split, structured event"
TMP=$(mktemp -d /tmp/t8-recovery-XXXX)
ORCH_HOME="$TMP/home" PYTHONPATH="$WT/src" "$PY" - "$TMP" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

from orchestrator.services import git_service
from orchestrator.utils import structured_log

tmp = Path(sys.argv[1])
repo = tmp / "repo"
repo.mkdir()
env = {
    **os.environ,
    "GIT_AUTHOR_NAME": "w",
    "GIT_AUTHOR_EMAIL": "w@w",
    "GIT_COMMITTER_NAME": "w",
    "GIT_COMMITTER_EMAIL": "w@w",
}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True)


git(repo, "init", "-q", "-b", "main")
(repo / "app.py").write_text("x = 1\n")
(repo / "docs").mkdir()
(repo / "docs" / "frozen-contracts.md").write_text("# frozen\n")
git(repo, "add", "-A")
git(repo, "commit", "-q", "-m", "base")
wt = tmp / "wt"
git(repo, "worktree", "add", "-q", "-b", "phase-1-demo", str(wt))

# the run-5 shape: an unfinished edit to tracked content, scratch, and a contract widening
(wt / "app.py").write_text("def probe():\n    return GitError('boom')\n")
(wt / "notes").mkdir()
(wt / "notes" / "scratch.txt").write_text("thinking out loud\n")
(wt / "docs" / "frozen-contracts.md").write_text("# frozen\n\n- quietly widened\n")

info = git_service.recovery_commit_info(wt, "uncommitted worker output from R-demo", "R-demo")
assert info is not None
print("  subject :", git(wt, "log", "-1", "--format=%s").stdout.strip())
body = git(wt, "log", "-1", "--format=%B").stdout
print("  trailer :", repr(git_service.recovery_run_of(body)))
print("  commit body:")
for line in body.strip().splitlines():
    print("    |", line)

details = git_service.recovery_event_details("worker left uncommitted changes", info)
payload = git_service.parse_recovery_event_details(details)
print("  build-log details (frozen line shape; structured payload inside `details`):")
print("   ", details)
print("  parsed:", payload)
print("  detected by the reader:", [c.short for c in git_service.recovery_commits(wt, "main")])
print("  next-run prompt listing:")
for line in git_service.format_recovery_commit_list(git_service.recovery_commits(wt, "main")).splitlines():
    print("    |", line)

# (c) DoD 9: an UNTRACKED-ONLY worktree is merge-clean but still swept, and reported separately
(wt / "just-scratch.txt").write_text("scratch only\n")
split = git_service.status_split(wt)
print("  untracked-only split:", split.as_dict())
print("  is_dirty (sweep/cleanup)   =", git_service.is_dirty(wt))
print("  tracked_dirty (merge/doctor)=", git_service.tracked_dirty(wt))
second = git_service.recovery_commit_info(wt, "leftover changes from R-demo2", "R-demo2")
print("  swept anyway:", second.as_dict() if second else None)
assert structured_log.parse(git_service.recovery_event_details("s", second))["recovery"] is True
PY
echo "  worktree left behind on purpose? no -> $(rm -rf "$TMP" && echo removed)"

echo
echo "### (c) the two predicates in the shipped call sites (grep of the decisions they gate)"
grep -n "tracked_dirty\|is_dirty" src/orchestrator/services/git_service.py src/orchestrator/services/project_service.py | grep -v "^src/orchestrator/services/git_service.py:1[0-9]:" | sed 's/^/  /'
