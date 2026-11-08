#!/usr/bin/env bash
# Regenerate verify-evidence/T-36/*.txt. Run from the repo root at the code HEAD you want to
# certify: the stamp excludes verify-evidence/T-36/ itself, so the entry count describes the
# branch that was actually tested and a rerun does not dirty the stamp. Outputs are staged in a
# temp dir and copied in afterwards; the evidence commit changes only verify-evidence/T-36/.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-36"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/t36-evidence.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
DIRTY="$(git status --porcelain -uall | grep -v '^.. verify-evidence/T-36/' | wc -l | tr -d ' ' || true)"
STAMP="# HEAD=$SHA entries-outside-verify-evidence/T-36=$DIRTY"
COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN="${COMMON%/.git}"
PY="$MAIN/.venv/bin/python"
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|FAILED|ERROR|All checks|BUDGET_|FLOOR_'

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/lint.txt"

{ echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test tests/unit/test_review_budget.py -vv -k"
  echo "#            'r68 or forty or floor or budget_rendered or over_budget or rendering_the_same'"
  ./scripts/orch_verify.sh test tests/unit/test_review_budget.py -vv \
    -k "r68 or forty or floor or budget_rendered or over_budget or rendering_the_same"; } 2>&1 \
  | grep -E "$KEEP|::test_|^# (HEAD|command)" > "$STAGE/targeted_tests.txt"

# The allocator run over THIS branch's own diff at the default budget: the DoD 1 invariant is only
# meaningful if it holds on the shape this task is actually shipped as.
{ echo "$STAMP"
  echo "# command: PYTHONPATH=src $PY (render_diff_budget over 'git diff <merge-base>...HEAD')"
  env -u VIRTUAL_ENV PYTHONPATH="$ROOT/src" "$PY" - <<'PY'
import subprocess

from orchestrator.services import prompt_builder as pb
from orchestrator.utils.truncate import estimate_tokens

base = subprocess.run(
    ["git", "merge-base", "main", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()
patch = subprocess.run(
    ["git", "diff", f"{base}...HEAD"], capture_output=True, text=True, check=True
).stdout
budget = pb.get_settings().max_diff_tokens
recorder = pb.TruncationRecorder()
rendered = pb.render_diff_budget(patch, budget, recorder)
files = pb.split_diff_files(patch)
shown = dict(pb.split_diff_files(rendered))
print(f"BUDGET_default={budget} patch_tokens={estimate_tokens(patch)} "
      f"files={len(files)} rendered_tokens={estimate_tokens(rendered)}")
for path, _chunk in files:
    chunk = shown.get(path, "")
    hunks = sum(1 for line in chunk.splitlines() if line.startswith("@@"))
    print(f"FLOOR_file {path} hunks_in_supplied={hunks} bytes={len(chunk.encode())}")
print(rendered.splitlines()[0])
missing = [p for p, _c in files if not shown.get(p)]
print(f"BUDGET_files_with_zero_supplied_bytes={len(missing)} {missing}")
zero_hunk = [
    p for p, _c in files if not any(x.startswith("@@") for x in shown.get(p, "").splitlines())
]
print(f"BUDGET_files_without_a_hunk={len(zero_hunk)} {zero_hunk}")
PY
} > "$STAGE/own_branch_diff_supply.txt" 2>&1

cp "$STAGE"/*.txt "$OUT"/
printf 'regenerated %s/*.txt at %s (entries outside the evidence dir: %s)\n' "$OUT" "$SHORT" "$DIRTY"