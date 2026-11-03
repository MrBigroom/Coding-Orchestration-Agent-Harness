#!/usr/bin/env bash
# orch_verify.sh — run this project's verification commands from ANY git worktree.
#
# Why this exists (task T-6). A gated run executes the project's test command with
# cwd = the task worktree, but `git worktree add` copies TRACKED files only, so a
# worktree has no `.venv`. Two consequences:
#   1. a venv-relative command (`make test`, `.venv/bin/pytest`) cannot run at all in
#      a worktree -> exit 127 -> verification fail -> requeue loop -> BLOCKED;
#   2. worse, this repo's `.venv` is an EDITABLE install (`_editable_impl_*.pth`
#      pointing at the main checkout), so reusing that interpreter from a worktree
#      makes `import orchestrator` resolve to the MAIN checkout: the suite goes green
#      while testing code that is not the branch under review — a false pass, which is
#      exactly what the independent verification gate exists to prevent.
# PYTHONPATH takes precedence over the editable `.pth`, so pointing it at the WORKTREE
# `src` is sufficient — but the resolution is ASSERTED below, never assumed: the
# wrapper prints the resolved module path and exits non-zero if it is not under the
# worktree root.
#
# Usage: scripts/orch_verify.sh <mode> [extra tool args...]
#   resolve   resolve + assert the import path, run nothing
#   test      python -m pytest <worktree>/tests  (extra args: -k/-x flags are added to
#             that target, an existing path replaces it — always relative to the root)
#   lint      python -m ruff check src tests
#   all       test then lint (both run even if the first fails)
#
# Environment overrides (all optional; nothing is installed and no network is used):
#   ORCH_VERIFY_VENV     venv to take the interpreter from (default <main repo>/.venv)
#   ORCH_VERIFY_PACKAGE  module whose path proves which checkout is under test
#                        (default orchestrator)
#   ORCH_VERIFY_SRC      PYTHONPATH root inside the worktree (default <root>/src,
#                        falling back to <root> when there is no src layout)
#   ORCH_VERIFY_LINT     lint argv (default: ruff check src tests)
#
# The inherited VIRTUAL_ENV is IGNORED on purpose: when the orchestrator server runs
# this from the main checkout, VIRTUAL_ENV (it is on the child-env allowlist) points at
# the main checkout venv, which is precisely the wrong thing to trust here.
#
# Exit codes: 0 ok · 2 usage error · 3 import resolved OUTSIDE the worktree (guard
# tripped) · 4 unusable repository/venv/interpreter · otherwise the tool's own code.
#
# Machine-readable marker line, parsed by `orchestrator project doctor --run-commands`
# (pipe-delimited, so paths containing spaces survive):
#   ORCH_VERIFY|mode=<mode>|module=<name>|module_file=<abs>|worktree_root=<abs>
set -euo pipefail

fail() {
  printf 'orch-verify: ERROR: %s\n' "$1" >&2
  exit "${2:-1}"
}

MODE="${1:-}"
[ $# -gt 0 ] && shift
EXTRA_ARGS=("$@")

case "$MODE" in
  resolve | test | lint | all) ;;
  *)
    printf 'usage: scripts/orch_verify.sh <resolve|test|lint|all> [extra args...]\n' >&2
    exit 2
    ;;
esac

git rev-parse --git-dir >/dev/null 2>&1 || fail "cwd ($(pwd)) is not inside a git repository" 4
ROOT="$(git rev-parse --show-toplevel)" || fail "cannot resolve the worktree root" 4

# The main repo: --path-format=absolute --git-common-dir gives <main>/.git even from a
# linked worktree. Anything unusual (bare-ish layout, git < 2.36) falls back to the
# first entry of `git worktree list`, which is the main checkout.
COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
MAIN_REPO=""
if [ -n "$COMMON" ]; then
  case "$COMMON" in
    */.git) MAIN_REPO="${COMMON%/.git}" ;;
  esac
fi
[ -n "$MAIN_REPO" ] || MAIN_REPO="$(git worktree list --porcelain | sed -n 's/^worktree //p; q')"
[ -n "$MAIN_REPO" ] || fail "cannot resolve the main repository" 4

VENV="${ORCH_VERIFY_VENV:-$MAIN_REPO/.venv}"
PY=""
for cand in "$VENV/bin/python" "$VENV/bin/python3"; do
  if [ -x "$cand" ]; then
    PY="$cand"
    break
  fi
done
[ -n "$PY" ] || fail "no interpreter in $VENV (run make setup, or set ORCH_VERIFY_VENV)" 4

SRC="${ORCH_VERIFY_SRC:-$ROOT/src}"
[ -d "$SRC" ] || SRC="$ROOT"

# cd BEFORE the resolution probe: `python -` and `python -m <tool>` both put the current
# directory first on sys.path, so the probe must run in exactly the directory the tool will
# run in, otherwise the asserted resolution is not the executed one.
cd "$ROOT"

# The fix, plus the hygiene the child must not inherit.
unset VIRTUAL_ENV
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$SRC${PYTHONPATH:+:$PYTHONPATH}"
export ORCH_VERIFY_PACKAGE="${ORCH_VERIFY_PACKAGE:-orchestrator}"

# ASSERT the resolution instead of assuming it (find_spec does not execute the module).
RESOLVED="$("$PY" - <<'PY_PROBE' 2>/dev/null || true
import importlib.util
import os
import sys

name = os.environ["ORCH_VERIFY_PACKAGE"]
try:
    spec = importlib.util.find_spec(name)
except Exception as exc:  # any import failure is a resolution failure
    print(f"import-error: {exc}", file=sys.stderr)
    sys.exit(5)
origin = getattr(spec, "origin", None) if spec else None
if not origin or origin == "None":
    print("not-found", file=sys.stderr)
    sys.exit(5)
print(origin)
PY_PROBE
)"
[ -n "$RESOLVED" ] ||
  fail "could not resolve module '${ORCH_VERIFY_PACKAGE}' with $PY (PYTHONPATH=$PYTHONPATH)" 3

printf 'orch-verify: worktree root %s\n' "$ROOT"
printf 'orch-verify: main repo     %s\n' "$MAIN_REPO"
printf 'orch-verify: venv python   %s\n' "$PY"
printf 'orch-verify: PYTHONPATH    %s\n' "$PYTHONPATH"
printf 'orch-verify: test root     %s\n' "$ROOT"
printf 'orch-verify: %s.__file__ = %s\n' "$ORCH_VERIFY_PACKAGE" "$RESOLVED"

case "$RESOLVED" in
  "$ROOT"/*) ;;
  *)
    printf 'ORCH_VERIFY|mode=%s|module=%s|module_file=%s|worktree_root=%s\n' \
      "$MODE" "$ORCH_VERIFY_PACKAGE" "$RESOLVED" "$ROOT" >&2
    fail "$ORCH_VERIFY_PACKAGE resolved OUTSIDE the worktree: $RESOLVED (root: $ROOT) — a run from here would verify the wrong checkout" 3
    ;;
esac
printf 'ORCH_VERIFY|mode=%s|module=%s|module_file=%s|worktree_root=%s\n' \
  "$MODE" "$ORCH_VERIFY_PACKAGE" "$RESOLVED" "$ROOT"

# Collection target: always the worktree, never whatever the invoking directory holds.
# Extra args that name an existing path replace that target (paths are relative to the
# worktree root); pure flags (-k, -x, …) are added to it.
TARGET="$ROOT/tests"
[ -d "$TARGET" ] || TARGET="$ROOT"
if [ ${#EXTRA_ARGS[@]} -eq 0 ]; then
  TEST_ARGS=("$TARGET")
else
  replaces=0
  for a in "${EXTRA_ARGS[@]}"; do
    case "$a" in
      -*) ;;
      *) if [ -e "$a" ]; then replaces=1; fi ;;
    esac
  done
  if [ "$replaces" = 1 ]; then
    TEST_ARGS=("${EXTRA_ARGS[@]}")
  else
    TEST_ARGS=("$TARGET" "${EXTRA_ARGS[@]}")
  fi
fi

lint_argv() {
  read -r -a LINT_ARGS <<<"${ORCH_VERIFY_LINT:-ruff check src tests}"
}

case "$MODE" in
  resolve) exit 0 ;;
  test)
    printf 'orch-verify: pytest target %s\n' "${TEST_ARGS[*]}"
    exec "$PY" -m pytest "${TEST_ARGS[@]}"
    ;;
  lint)
    lint_argv
    printf 'orch-verify: lint target  %s\n' "${LINT_ARGS[*]}"
    exec "$PY" -m "${LINT_ARGS[@]}"
    ;;
  all)
    lint_argv
    printf 'orch-verify: pytest target %s\n' "${TEST_ARGS[*]}"
    printf 'orch-verify: lint target  %s\n' "${LINT_ARGS[*]}"
    rc=0
    "$PY" -m pytest "${TEST_ARGS[@]}" || rc=$?
    "$PY" -m "${LINT_ARGS[@]}" || rc=$((rc == 0 ? 1 : rc))
    exit "$rc"
    ;;
esac
