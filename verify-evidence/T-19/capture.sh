#!/usr/bin/env bash
# Regenerate verify-evidence/T-19/*.txt. Run from the repo root at the code HEAD you want to
# certify; outputs are staged in a temp dir so the worktree stays clean while the parity tests
# check `git status` for themselves. Evidence-only files never change the tested HEAD.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-19"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/t19-evidence.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
SHA="$(git rev-parse --short HEAD)"
DIRTY="$(git status --porcelain -uno | grep -v '^.. verify-evidence/T-19/' | wc -l | tr -d ' ' || true)"
STAMP="# HEAD=$SHA tracked-dirty-entries=$DIRTY"
COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN="${COMMON%/.git}"
PY="$MAIN/.venv/bin/python"
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|SKIPPED|All checks|MUTATION_|DEVELOPER CHECK|seeded env file|nothing to seed|stale worktree'

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/lint.txt"

{ echo "$STAMP"
  echo "# command: ORCH_ENV_SPAWN_TESTS=1 ./scripts/orch_verify.sh test -vv -k 'env_guard or env_hermeticity'"
  ORCH_ENV_SPAWN_TESTS=1 ./scripts/orch_verify.sh test -vv -k "env_guard or env_hermeticity"; } 2>&1 \
  | grep -E "$KEEP|::test_|^# (HEAD|command)" > "$STAGE/t19_tests.txt"

{ echo "$STAMP"
  echo "# command: PYTHONPATH=src $PY - <<'PY' (mutation: replace AMBIENT_ENV with the old provider tuple)"
  env -u VIRTUAL_ENV PYTHONPATH="$ROOT/src:$ROOT" "$PY" - <<'PY'
from orchestrator.core.config import PROVIDER_ENV_NAMES, settings_env_names
from tests import conftest


def coverage_is_clean() -> bool:
    # The exact predicate tests/unit/test_env_guard.py pins for DoD 1.
    return conftest.env_names_not_covered() == set()


derived_clean = coverage_is_clean()
conftest.AMBIENT_ENV = PROVIDER_ENV_NAMES  # the old hand-kept clear-list
mutated_clean = coverage_is_clean()
gaps = conftest.env_names_not_covered()
print(f"derived settings env names: {len(settings_env_names())}")
print(f"coverage clean with the derivation: {derived_clean}")
print(f"coverage clean after removing it: {mutated_clean}")
for name in ("ORCHESTRATOR_SUPABASE_URL", "ORCHESTRATOR_SUPABASE_PASSWORD", "ORCHESTRATOR_TOKEN"):
    print(f"  {name} in gaps: {name in gaps}")
assert derived_clean and not mutated_clean
print("MUTATION_DETECTED: removing the Settings derivation fails the coverage test")
PY
} > "$STAGE/coverage_mutation.txt" 2>&1

SEED="$STAGE/synthetic.env"
{
  printf '%s=%s\n' ORCHESTRATOR_SUPABASE_URL https://synthetic.invalid
  printf '%s=%s\n' ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY not-a-secret-0000
  printf '%s=%s\n' ORCHESTRATOR_SUPABASE_EMAIL synthetic@example.invalid
  printf '%s=%s\n' ORCHESTRATOR_SUPABASE_PASSWORD not-a-secret-0001
  printf '%s=%s\n' ANTHROPIC_API_KEY not-a-secret-0002
} > "$SEED"
{ echo "$STAMP"
  echo "# command: ORCH_PARITY_ENV_FILE=<synthetic .env> PYTEST_ADDOPTS='-k env_guard' make test-parity"
  ORCH_PARITY_ENV_FILE="$SEED" PYTEST_ADDOPTS="-k env_guard" make test-parity; } 2>&1 \
  | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/make_test_parity.txt"

cp "$STAGE"/*.txt "$OUT"/
printf 'regenerated %s/*.txt at %s (tracked-dirty-entries=%s)\n' "$OUT" "$SHA" "$DIRTY"