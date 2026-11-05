#!/usr/bin/env bash
# T-19 capture at the code HEAD. Run from the phase worktree while it is clean (no tracked edits).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"; cd "$ROOT"
OUT="$ROOT/verify-evidence/T-19"; mkdir -p "$OUT"
STAMP="# HEAD=$(git rev-parse HEAD) tracked-dirty-entries=$(git status --porcelain -uno | wc -l | tr -d ' ')"
{ printf '%s\n' "$STAMP"; ./scripts/orch_verify.sh test | tail -1; } > "$OUT/full_suite.txt"
{ printf '%s\n' "$STAMP"; ./scripts/orch_verify.sh lint | tail -1; } > "$OUT/lint.txt"
{ printf '%s\n' "$STAMP"
  ./scripts/orch_verify.sh test -vv -k "(env_guard or env_hermeticity) and not full_suite_is_identical" \
    | grep -E 'PASSED|SKIPPED|passed'; } > "$OUT/t19_tests.txt"
{ printf '%s\n' "$STAMP"
  ./scripts/orch_verify.sh test -vv -k "full_suite_is_identical" \
    | grep -E 'PASSED|FAILED|passed'; } > "$OUT/full_suite_parity.txt"
{ printf '%s\n' "$STAMP"
  PYTHONPATH="$ROOT/src" "$(git rev-parse --path-format=absolute --git-common-dir | sed 's#/\.git$##')/.venv/bin/python" - <<'PY'
from orchestrator.core.config import PROVIDER_ENV_NAMES, settings_env_names
from tests import conftest
gaps = sorted(conftest.env_names_not_covered(PROVIDER_ENV_NAMES))
print(f"derived settings env names: {len(settings_env_names())}")
print("T-9/leak names reported as gaps when the Settings derivation is removed:")
for name in ("ORCHESTRATOR_SUPABASE_URL", "ORCHESTRATOR_SUPABASE_EMAIL",
             "ORCHESTRATOR_SUPABASE_PASSWORD", "ORCHESTRATOR_TOKEN"):
    assert name in gaps
    print(f"  {name}")
assert not conftest.env_names_not_covered()
print("MUTATION_DETECTED: removing the Settings derivation fails the coverage check")
PY
} > "$OUT/coverage_mutation.txt"
