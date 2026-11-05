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
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|SKIPPED|All checks|MUTATION_DETECTED|DEVELOPER CHECK|seeded env file|nothing to seed'

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/lint.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test -vv -k 'env_guard or env_hermeticity'"
  ./scripts/orch_verify.sh test -vv -k "env_guard or env_hermeticity"; } 2>&1 \
  | grep -E "$KEEP|::test_|^# (HEAD|command)" > "$STAGE/t19_tests.txt"

{ echo "$STAMP"
  echo "# command: ORCH_ENV_PARITY_FULL=1 ./scripts/orch_verify.sh test -vv -k full_suite_is_identical"
  ORCH_ENV_PARITY_FULL=1 ./scripts/orch_verify.sh test -vv -k full_suite_is_identical; } 2>&1 \
  | grep -E "$KEEP|::test_|^# (HEAD|command)" > "$STAGE/full_suite_parity.txt"

{ echo "$STAMP"
  echo "# command: PYTHONPATH=src $PY - <<'PY' (mutation: replace AMBIENT_ENV with the old provider tuple)"
  env -u VIRTUAL_ENV PYTHONPATH="$ROOT/src:$ROOT" "$PY" - <<'PY'
from orchestrator.core.config import PROVIDER_ENV_NAMES
from tests import conftest

derived = conftest.AMBIENT_ENV
conftest.AMBIENT_ENV = PROVIDER_ENV_NAMES  # the old hand-kept clear-list
gaps = conftest.env_names_not_covered()
conftest.AMBIENT_ENV = derived
print(f"derived settings env names: {len(derived)}")
print(f"coverage gaps reported with the hand-kept list: {len(gaps)}")
for name in ("ORCHESTRATOR_SUPABASE_URL", "ORCHESTRATOR_SUPABASE_PASSWORD", "ORCHESTRATOR_TOKEN"):
    print(f"  {name} in gaps: {name in gaps}")
assert gaps, "mutation not detected"
print("MUTATION_DETECTED: removing the Settings derivation fails the coverage check")
PY
} > "$STAGE/coverage_mutation.txt" 2>&1

SEED="$STAGE/synthetic.env"
cat > "$SEED" <<'ENV'
ORCHESTRATOR_SUPABASE_URL=https://synthetic.invalid
ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY=SYNTHETIC_PLACEHOLDER
ORCHESTRATOR_SUPABASE_EMAIL=synthetic@example.invalid
ORCHESTRATOR_SUPABASE_PASSWORD=SYNTHETIC_PLACEHOLDER
ANTHROPIC_API_KEY=SYNTHETIC_PLACEHOLDER
ENV
{ echo "$STAMP"; echo "# command: ORCH_PARITY_ENV_FILE=<synthetic .env> make test-parity"
  ORCH_PARITY_ENV_FILE="$SEED" make test-parity; } 2>&1 \
  | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/make_test_parity.txt"

cp "$STAGE"/*.txt "$OUT"/
printf 'regenerated %s/*.txt at %s (tracked-dirty-entries=%s)\n' "$OUT" "$SHA" "$DIRTY"