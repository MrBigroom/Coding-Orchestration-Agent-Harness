#!/usr/bin/env bash
# Regenerate verify-evidence/T-22/*.txt. Run from the repo root at the code HEAD you want to
# certify; outputs are staged in a temp dir so the worktree stays clean while the suite runs, and
# the stamp describes the branch that was actually tested. Evidence-only files never change the
# HEAD under test: the code commit comes first, this script is run at it, and the evidence is
# committed afterwards (it touches only paths under verify-evidence/T-22/).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-22"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/t22-evidence.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
DIRTY="$(git status --porcelain -uall | grep -v '^.. verify-evidence/T-22/' | wc -l | tr -d ' ' || true)"
STAMP="# HEAD=$SHA tracked-and-untracked-entries-outside-this-dir=$DIRTY"
COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN="${COMMON%/.git}"
PY="$MAIN/.venv/bin/python"
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|SKIPPED|ERROR|All checks|MUTATION_|red:|expected at least'

echo "capturing at $SHORT (dirty entries outside the evidence dir: $DIRTY)"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/lint.txt"

{ echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test -vv tests/unit/test_absence_resolution.py"
  echo "#            tests/unit/test_review_audit.py tests/integration/test_review_payload.py"
  ./scripts/orch_verify.sh test -vv tests/unit/test_absence_resolution.py \
    tests/unit/test_review_audit.py tests/integration/test_review_payload.py; } 2>&1 \
  | grep -E "PASSED|FAILED|ERROR|passed|failed|^# (HEAD|command)|ORCH_VERIFY\|" > "$STAGE/t22_tests.txt"

for mode in index wiring replay mutation; do
  { echo "$STAMP"; env -u VIRTUAL_ENV PYTHONPATH="$ROOT/src:$ROOT" "$PY" \
      "verify-evidence/T-22/extractor.py" "--$mode"; } > "$STAGE/${mode/_/--}.txt" 2>&1
done
mv "$STAGE/index.txt" "$STAGE/resolver_index.txt"

# docs.txt: the DoD 6 sections, read from the committed docs at this HEAD. Ranges are found by
# marker so a docs edit that shifts line numbers cannot silently leave a stale excerpt behind.
B1=$(git show HEAD:docs/build-loop.md | grep -n '^- \*\*Resolve before judging' | cut -d: -f1)
B2=$(git show HEAD:docs/build-loop.md | grep -n '^## Verification artifacts' | cut -d: -f1)
D1=$(git show HEAD:docs/decisions.md | grep -n '2026-11-06' | cut -d: -f1)
D2=$(git show HEAD:docs/decisions.md | wc -l | tr -d ' ')
{
  echo "$STAMP"
  echo "# command: git show HEAD:docs/build-loop.md | nl -ba | sed -n '${B1},$((B2 - 1))p';" \
       "git show HEAD:docs/decisions.md | nl -ba | sed -n '${D1},${D2}p'"
  echo
  echo "## docs/build-loop.md (line numbers are source lines, not this file's lines)"
  git show HEAD:docs/build-loop.md | nl -ba | sed -n "${B1},$((B2 - 1))p"
  echo
  echo "## docs/decisions.md"
  git show HEAD:docs/decisions.md | nl -ba | sed -n "${D1},${D2}p"
} > "$STAGE/docs.txt"

cp "$STAGE"/*.txt "$OUT/"
echo "wrote: $(ls "$OUT" | tr '\n' ' ')"
echo "# done at $SHORT"
