#!/usr/bin/env bash
# T-6 DoD 3 proof: `orchestrator project doctor --run-commands` through the REAL CLI, in an
# isolated ORCHESTRATOR_HOME, against this repo. No network, no API keys, nothing installed.
#
# A. the trap as the operator hits it today: a venv-relative command exits 0 inside a
#    throwaway worktree while importing the MAIN checkout -> the probe must FLAG it, because
#    a green exit code alone is exactly the false pass the gate exists to catch.
# B. the fix: the committed wrapper -> exits 0 AND resolves inside the worktree.
# C. plain `doctor` (no flag) runs nothing and creates no worktree (safety default).
# D. pre-merge caveat: with base_branch=main the wrapper is not in the snapshot yet, so the
#    probe reports that instead of going green.
# E. nothing is left behind in ORCHESTRATOR_HOME.
set -uo pipefail
export TERM=dumb COLUMNS=200
MAIN=/run/media/mrbigroom/Backup2TB/Projects/Coding-Orchestration-Agent-Harness
WT=$(git rev-parse --show-toplevel)
PY=$MAIN/.venv/bin/python
CLI=$MAIN/.venv/bin/orchestrator
BR=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
export ORCHESTRATOR_HOME=/tmp/t6-dod3-home
export PYTHONPATH="$WT/src"   # the CLI itself must run the code under review
OUT=${1:-/tmp/t6-dod3.out}
exec >"$OUT" 2>&1
rm -rf "$ORCHESTRATOR_HOME"; mkdir -p "$ORCHESTRATOR_HOME"
cd "$WT" || exit 1

echo "== commit under test: $BR @ $(git rev-parse --short HEAD) =="
echo "   the CLI imports: $($PY -c 'import orchestrator; print(orchestrator.__file__)')"
$CLI init >/dev/null 2>&1; echo "   init exit=$?"

show() { grep -v '^\[' | sed 's/^/   /'; }

echo
echo "===== A. THE TRAP: venv-relative command, no wrapper ====="
$CLI project add t6-trap "$MAIN" --base-branch "$BR" \
  --test-command "$PY -m pytest tests/unit/test_paths.py" >/dev/null 2>&1
$CLI project doctor t6-trap --run-commands 2>&1 | show
echo "   doctor exit=${PIPESTATUS[0]}"

echo
echo "===== B. THE FIX: the committed wrapper (base branch = this task branch) ====="
$CLI project add t6-wrapped "$MAIN" --base-branch "$BR" \
  --test-command "scripts/orch_verify.sh test tests/unit/test_paths.py" \
  --lint-command "scripts/orch_verify.sh lint" >/dev/null 2>&1
$CLI project doctor t6-wrapped --run-commands 2>&1 | show
echo "   doctor exit=${PIPESTATUS[0]}"

echo
echo "===== C. plain doctor executes nothing (safety default) ====="
before=$(find "$ORCHESTRATOR_HOME/worktrees" -name doctor 2>/dev/null | wc -l)
$CLI project doctor t6-wrapped 2>&1 | show
after=$(find "$ORCHESTRATOR_HOME/worktrees" -name doctor 2>/dev/null | wc -l)
echo "   throwaway worktrees created by plain doctor: $((after - before)) (expect 0)"

echo
echo "===== D. pre-merge caveat: base_branch=main has no wrapper yet ====="
$CLI project add t6-mainbase "$MAIN" --base-branch main \
  --test-command "scripts/orch_verify.sh test tests/unit/test_paths.py" >/dev/null 2>&1
$CLI project doctor t6-mainbase --run-commands 2>&1 | tail -8 | show
echo "   doctor exit=${PIPESTATUS[0]}"

echo
echo "===== E. the wrapper refuses to run outside a git repository ====="
(cd /tmp && "$WT/scripts/orch_verify.sh" resolve >/dev/null 2>&1; echo "   exit=$? (4 = not a repo)")

echo
echo "===== F. nothing left behind ====="
echo "   surviving throwaway worktree dirs: $(find "$ORCHESTRATOR_HOME/worktrees" -name doctor -type d 2>/dev/null | wc -l) (expect 0)"
echo "   surviving .managed marker files:   $(find "$ORCHESTRATOR_HOME/worktrees" -path '*.managed/*' -type f 2>/dev/null | wc -l) (expect 0)"
echo "   worktrees known to git in the main repo:"
git -C "$MAIN" worktree list | sed 's/^/     /'
echo "DONE dod3"
