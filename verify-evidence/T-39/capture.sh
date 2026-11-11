#!/usr/bin/env bash
# Regenerate verify-evidence/T-39/*.txt.
#
# Do NOT run this directly: run `orchestrator task evidence T-39 --full` from a clean worktree.
# That driver refuses uncommitted inputs, executes this committed script, canonicalises the stamp
# on every file this pass rewrote to `# HEAD=<sha> mode=<fast|full>`, and writes manifest.json plus
# freshness_selfcheck.txt. The stamp names the code HEAD measured; the evidence-only commit that
# ships the bundle therefore does not stale it.
#
# SAFETY: the live section is a DRY RUN ONLY. The operator note for T-39 forbids --no-dry-run on
# the live home, and this script never passes it. The database is opened with sqlite mode=ro and
# `_ensure_ready()`/`db.upgrade()` are never called, so branch code cannot migrate the live
# database (pinned memory: never point a worktree CLI at the live ORCHESTRATOR_HOME; audit it
# read-only instead).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/t39-evidence.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
SHA="$(git rev-parse HEAD)"
MODE=fast
[ "${T15_FULL:-}" = "1" ] && MODE=full
STAMP="# HEAD=$SHA mode=$MODE"
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|FAILED|ERROR|All checks|^# (HEAD|command|exact)'

echo "capturing at $(git rev-parse --short HEAD) mode=$MODE"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/lint.txt"

{ echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test -vv tests/integration/test_cleanup_chat_worktrees.py"
  ./scripts/orch_verify.sh test -vv tests/integration/test_cleanup_chat_worktrees.py; } 2>&1 \
  | grep -E "PASSED|FAILED|ERROR|passed|failed|^# (HEAD|command)|ORCH_VERIFY\|" \
  > "$STAGE/t39_tests.txt"

# Source anchors from the committed HEAD (line numbers are source lines, not this file's).
{ echo "$STAMP"
  echo "# command: git show HEAD:<file> | grep -nE '<anchors>'"
  for spec in \
    "src/orchestrator/services/cleanup_service.py|def cwd_owner_pids|def _chat_sessions_by_cwd|def _chat_action|def plan|def counts|def summary|def as_dict|include_live_chats|live process|is closed \(pass --include-chats|process_is_alive|status != ChatStatus.closed" \
    "src/orchestrator/cli.py|def cleanup|--include-chats|--include-live-chats|cleanup_service.plan|soft_wrap=True|cleanup_service.FAILED" \
    "src/orchestrator/services/git_service.py|class RemovalOutcome|def process_is_alive|def remove_worktree_outcome|lock_free=dry_run|REMOVAL_FAILED|failed to remove worktree" \
    "src/orchestrator/utils/git.py|lock_free|no-optional-locks" \
    "src/orchestrator/core/models.py|class ChatStatus|busy = \"busy\"|closed = \"closed\"" \
    "src/orchestrator/services/chat_service.py|cwd=Path\(chat.cwd\)" \
    "docs/chat.md|## \`orchestrator cleanup\` and chat worktrees|judged by OWNERSHIP|live process" \
    "docs/build-loop.md|judges each worktree by|chat\*\* worktree|--include-live-chats"; do
    file="${spec%%|*}"; anchors="${spec#*|}"
    echo "## $file"
    git show "HEAD:$file" | grep -nE "$anchors" || true
  done
} > "$STAGE/source_anchors.txt"

# Live measurement: DRY RUN ONLY against the operator's real installation (if present). Proves the
# reported defect is closed on the machine where it was measured, without writing anything.
LIVE="${T39_LIVE_HOME:-$HOME/.local/share/coding-orchestrator}"
MAIN="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN="${MAIN%/.git}"
PY="$MAIN/.venv/bin/python"
[ -x "$PY" ] || PY=python3
{
  echo "$STAMP"
  echo "# DRY RUN ONLY (the T-39 operator note forbids --no-dry-run on the live home)."
  echo "# command: ORCHESTRATOR_HOME=$LIVE PYTHONPATH=$ROOT/src $PY - (cleanup_service.plan dry_run=True)"
  echo "# sqlite is opened with mode=ro; _ensure_ready()/db.upgrade() are NEVER called."
  if [ ! -f "$LIVE/orchestrator.db" ]; then
    echo "live home $LIVE has no orchestrator.db here; live measurement skipped"
  else
    markers_before=$(find "$LIVE/worktrees" -path '*/.managed/*' -type f 2>/dev/null | wc -l)
    list_before=$(git -C "$MAIN" worktree list --porcelain | grep -c '^worktree ')
    ORCHESTRATOR_HOME="$LIVE" PYTHONPATH="$ROOT/src" T39_WORKTREE="$ROOT" "$PY" - <<'PY'
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlmodel import Session

import orchestrator
from orchestrator.services import cleanup_service

root = os.environ["T39_WORKTREE"]
assert orchestrator.__file__.startswith(root), orchestrator.__file__
home = Path(os.environ["ORCHESTRATOR_HOME"])
print(f"import={orchestrator.__file__}")
print(f"live_home={home} db_mode=ro")
engine = create_engine(f"sqlite:///file:{home / 'orchestrator.db'}?mode=ro&uri=true")
with Session(engine) as s:
    actions = cleanup_service.plan(s, dry_run=True)
for action in actions:
    print(f"{action.kind} | {action.path} | {action.reason}")
print(cleanup_service.summary(actions, dry_run=True))
PY
    markers_after=$(find "$LIVE/worktrees" -path '*/.managed/*' -type f 2>/dev/null | wc -l)
    list_after=$(git -C "$MAIN" worktree list --porcelain | grep -c '^worktree ')
    echo "managed_markers_before=$markers_before after=$markers_after"
    echo "git_worktree_list_before=$list_before after=$list_after"
  fi
} > "$STAGE/live_measurement.txt" 2>&1

for f in full_suite.txt lint.txt t39_tests.txt source_anchors.txt live_measurement.txt; do
  mv "$STAGE/$f" "$OUT/$f"
done