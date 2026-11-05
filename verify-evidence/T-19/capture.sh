#!/usr/bin/env bash
# T-19 evidence capture. Run from the phase worktree at the code HEAD under review.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-19"
mkdir -p "$OUT"
HEAD_SHA="$(git rev-parse HEAD)"
DIRTY="$(git status --porcelain -uno | wc -l | tr -d ' ')"
STAMP="# HEAD=${HEAD_SHA} tracked-dirty-entries=${DIRTY} captured=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  printf '%s\n' "$STAMP"
  printf '# command: ./scripts/orch_verify.sh test  (full suite, at the code HEAD above)\n'
  cat /tmp/t19-final-suite.txt
} > "$OUT/full_suite.txt"

{
  printf '%s\n' "$STAMP"
  printf '# command: ./scripts/orch_verify.sh lint\n'
  cat /tmp/t19-final-lint.txt
} > "$OUT/lint.txt"

{
  printf '%s\n' "$STAMP"
  printf '# command: ./scripts/orch_verify.sh test -vv -k "(env_guard or env_hermeticity) and not full_suite_is_identical"\n'
  ./scripts/orch_verify.sh test -vv \
    -k "(env_guard or env_hermeticity) and not full_suite_is_identical"
} > "$OUT/t19_tests.txt" 2>&1

{
  printf '%s\n' "$STAMP"
  printf '# Mutation check: the coverage predicate with the OLD hand-kept list (provider keys only).\n'
  PYTHONPATH="$ROOT/src" ORCH_VERIFY_PACKAGE=orchestrator "$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir | sed 's#/\.git$##')/.venv/bin/python" - <<'PY'
from orchestrator.core.config import PROVIDER_ENV_NAMES, settings_env_names
from tests import conftest

missing = sorted(conftest.env_names_not_covered(PROVIDER_ENV_NAMES))
print(f"derived settings env names: {len(settings_env_names())}")
print(f"hand-kept list coverage gaps: {missing}")
assert "ORCHESTRATOR_SUPABASE_URL" in missing
assert "ORCHESTRATOR_SUPABASE_PASSWORD" in missing
assert "ORCHESTRATOR_TOKEN" in missing
print("MUTATION_DETECTED: removing the Settings derivation fails the coverage check")
PY
} > "$OUT/coverage_mutation.txt" 2>&1

cp /tmp/t19_capture.sh "$OUT/capture.sh"
chmod +x "$OUT/capture.sh"
printf 'evidence written under %s\n' "$OUT"
