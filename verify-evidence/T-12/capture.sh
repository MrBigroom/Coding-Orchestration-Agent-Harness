#!/usr/bin/env bash
# T-12 evidence capture: one invocation, one stamped HEAD, outputs staged in /tmp and moved
# into verify-evidence/T-12 only after every command has run, so the stamp really does record
# dirty-entries=0 at the start instead of counting the evidence files written by this script.
#
# Round-1 review found a dirty-entries=1 stamp and a test diff truncated after l.138, so this
# script also commits the full test source and the git file listing it refers to.
#
# Re-run:  bash verify-evidence/T-12/capture.sh
set -uo pipefail
cd "$(dirname "$0")/../.."

WT=$(pwd)
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
HEAD=$(git rev-parse HEAD)
STAMP="# HEAD=$HEAD ($(git rev-parse --short HEAD)) branch=$(git branch --show-current) captured=$(date -Is) tree=$(git status --porcelain | wc -l) dirty-entries"
TMP=$(mktemp -d /tmp/t12-evidence.XXXXXX)
TMPW=/tmp/t12-web-typecheck
trap 'rm -rf "$TMP" "$TMPW"' EXIT

echo "$STAMP"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test   (whole tests/ tree of THIS worktree)"
  ./scripts/orch_verify.sh test
  echo "SUITE_EXIT=$?"
} >"$TMP/full_suite.txt"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test tests/integration/test_attempt_cap.py -vv"
  ./scripts/orch_verify.sh test tests/integration/test_attempt_cap.py -vv
  echo "TARGETED_EXIT=$?"
} >"$TMP/attempt_cap_tests.txt"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh lint   (ruff check src tests)"
  ./scripts/orch_verify.sh lint
  echo "LINT_EXIT=$?"
} >"$TMP/lint.txt"

{
  echo "$STAMP"
  echo "# source: tests/integration/test_attempt_cap.py at $HEAD, full file with line numbers."
  echo "# The round-1 reviewer payload truncated this diff after l.138; this file is the whole"
  echo "# source the passing counts above refer to, not a summary of it."
  nl -ba tests/integration/test_attempt_cap.py
} >"$TMP/attempt_cap_source.txt"

{
  echo "$STAMP"
  echo "# git proof for every file this phase claims, including web/src/pages/Tasks.tsx, which"
  echo "# the round-1 review could not place in either commit's file list."
  echo
  echo "## git show --stat HEAD (this capture script commit)"
  git show --stat --oneline HEAD
  echo
  echo "## git show --stat 486def2 (round-2 fixes: submission status in requeue/block reasons)"
  git show --stat --oneline 486def2
  echo
  echo "## git show --stat a3eb2fd (implementation)"
  git show --stat --oneline a3eb2fd
  echo
  echo "## git ls-tree HEAD -- every claimed file"
  git ls-tree HEAD -- \
    README.md docs/build-loop.md docs/configuration.md \
    src/orchestrator/api/routes/tasks.py src/orchestrator/services/prompt_builder.py \
    src/orchestrator/services/run_service.py src/orchestrator/services/settings_service.py \
    src/orchestrator/services/supervisor_service.py tests/integration/test_attempt_cap.py \
    web/src/api/types.ts web/src/pages/Settings.tsx web/src/pages/TaskDetail.tsx \
    web/src/pages/Tasks.tsx
  echo
  echo "## Tasks.tsx at HEAD: the attempts column this phase added"
  git show HEAD:web/src/pages/Tasks.tsx |
    grep -n 'Attempts\|attempts.summary\|attempts.max_run_attempts_per_task'
  echo
  echo "## no new dependencies: diff of pyproject.toml/uv.lock against the merge-base"
  BASE=$(git merge-base HEAD main)
  git diff --stat "$BASE"..HEAD -- pyproject.toml uv.lock
  echo "(empty above means neither file changed)"
} >"$TMP/head_files.txt"

{
  echo "$STAMP"
  echo "# command: cold python process, imported from THIS worktree, clean env defaults"
  env -u VIRTUAL_ENV -u ORCHESTRATOR_ENABLE_WORKER -u ORCHESTRATOR_ENABLE_SCHEDULER \
    ORCHESTRATOR_HOME="$TMP/home" PYTHONPATH="$WT/src" \
    "$MAIN/.venv/bin/python" - <<'PY'
from orchestrator.services import settings_service

for key, value in sorted(settings_service.defaults().items()):
    print(f"{key}={value!r}")
PY
  echo "DEFAULTS_EXIT=$?"
} >"$TMP/settings_defaults.txt"

# Type-check this worktree's web/src through a scratch symlink tree: a worktree has no
# node_modules, and the symlink must NOT live inside the worktree or it becomes untracked dirt.
rm -rf "$TMPW"
mkdir -p "$TMPW"
ln -s "$MAIN/web/node_modules" "$TMPW/node_modules"
for f in src index.html package.json tsconfig.json vite.config.ts tailwind.config.js postcss.config.js; do
  ln -s "$WT/web/$f" "$TMPW/$f"
done
{
  echo "$STAMP"
  echo "worktree       : $WT"
  echo "main repo      : $MAIN"
  echo "scratch tree   : $TMPW"
  echo "web/src link   : $(readlink "$TMPW/src")   # resolves into THIS worktree"
  echo
  echo "# command: cd $TMPW && ./node_modules/.bin/tsc -b --force"
  (cd "$TMPW" && ./node_modules/.bin/tsc -b --force)
  echo "TSC_EXIT=$?"
  echo
  echo "# command: tsc -p tsconfig.json --noEmit --listFiles | grep the T-12 GUI files"
  (cd "$TMPW" && ./node_modules/.bin/tsc -p tsconfig.json --noEmit --listFiles) |
    grep -E "$TMPW/src/(api/types|pages/(Tasks|TaskDetail|Settings))\.tsx?$"
  FILES=$(cd "$TMPW" && ./node_modules/.bin/tsc -p tsconfig.json --noEmit --listFiles |
    grep -c "$TMPW/src/")
  echo "$FILES source files type-checked, all resolved through the worktree symlink"
} >"$TMP/gui_typecheck.txt"

for f in full_suite.txt attempt_cap_tests.txt lint.txt attempt_cap_source.txt head_files.txt \
  settings_defaults.txt gui_typecheck.txt; do
  mv "$TMP/$f" "verify-evidence/T-12/$f"
done

grep -E "^(#|[0-9]+ (passed|failed|skipped)|[0-9]+ error|All checks|SUITE_EXIT|TARGETED_EXIT|LINT_EXIT|TSC_EXIT|DEFAULTS_EXIT)" \
  verify-evidence/T-12/full_suite.txt \
  verify-evidence/T-12/attempt_cap_tests.txt \
  verify-evidence/T-12/lint.txt \
  verify-evidence/T-12/gui_typecheck.txt \
  verify-evidence/T-12/settings_defaults.txt