#!/usr/bin/env bash
# T-12 evidence capture: one invocation, one stamped HEAD. Outputs are staged in /tmp and moved
# into verify-evidence/T-12 only after every command ran, so the dirty-entries stamp is real.
# Evidence files are marked `-diff` in .gitattributes: they are committed, but the review diff
# budget is spent on the source and test bodies instead of on pasted logs.
# Re-run: bash verify-evidence/T-12/capture.sh
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
  echo

  echo "# command: cold python, imported from THIS worktree, clean env defaults"
  env -u VIRTUAL_ENV -u ORCHESTRATOR_ENABLE_WORKER -u ORCHESTRATOR_ENABLE_SCHEDULER \
    ORCHESTRATOR_HOME="$TMP/home" PYTHONPATH="$WT/src" \
    "$MAIN/.venv/bin/python" - <<'PY'
from orchestrator.services import settings_service

for key, value in sorted(settings_service.defaults().items()):
    print(f"{key}={value!r}")
PY
  echo "DEFAULTS_EXIT=$?"
  echo

  # Type-check this worktree's web/src through a scratch symlink tree: a worktree has no
  # node_modules, and the link must NOT live inside the worktree or it becomes untracked dirt.
  rm -rf "$TMPW"
  mkdir -p "$TMPW"
  ln -s "$MAIN/web/node_modules" "$TMPW/node_modules"
  for f in src index.html package.json tsconfig.json vite.config.ts tailwind.config.js postcss.config.js; do
    ln -s "$WT/web/$f" "$TMPW/$f"
  done
  echo "web/src link: $(readlink "$TMPW/src")   # resolves into THIS worktree"
  echo "# command: cd $TMPW && ./node_modules/.bin/tsc -b --force"
  (cd "$TMPW" && ./node_modules/.bin/tsc -b --force)
  echo "TSC_EXIT=$?"
  (cd "$TMPW" && ./node_modules/.bin/tsc -p tsconfig.json --noEmit --listFiles) |
    grep -E "$TMPW/src/(api/types|pages/(Tasks|TaskDetail|Settings))\.tsx?$"
} >"$TMP/checks.txt"

for f in full_suite.txt attempt_cap_tests.txt checks.txt; do
  mv "$TMP/$f" "verify-evidence/T-12/$f"
done

grep -E "^(#|[0-9]+ (passed|failed|skipped)|[0-9]+ error|All checks|SUITE_EXIT|TARGETED_EXIT|LINT_EXIT|TSC_EXIT|DEFAULTS_EXIT)" \
  verify-evidence/T-12/full_suite.txt \
  verify-evidence/T-12/attempt_cap_tests.txt \
  verify-evidence/T-12/checks.txt