#!/usr/bin/env bash
# orch_env_parity.sh — DEVELOPER parity check, never the gate command.
#
# The gate runs the project's commands in a tracked-files-only worktree: no `.env`, no
# node_modules, no `.venv`. That is deliberate (and T-19 records it in verification.json), but
# it means a gate green and a red run in your own checkout can both be true. This script asks
# the other question:
#
#     "does the suite still pass when my local .env is present?"
#
# It creates a THROWAWAY detached worktree of the current HEAD, copies the local `.env` into it
# (contents are never printed, staged or committed), and runs the same path-independent wrapper
# the gate uses. It never writes to the operator checkout and it is never part of
# `make test`/`make verify` or a verification command.
#
# Usage: scripts/orch_env_parity.sh [test|lint|all] [wrapper args...]
#   Default mode is `test`. Extra args are passed to scripts/orch_verify.sh.
#
# Environment:
#   ORCH_PARITY_ENV_FILE   env file to seed (default <repo root>/.env)
#
# Exit codes: 0 parity run green · 4 no env file to seed / not a repository ·
#             otherwise scripts/orch_verify.sh's own exit code.
set -euo pipefail

MODE="${1:-test}"
[ $# -gt 0 ] && shift
EXTRA_ARGS=("$@")

fail() {
  printf 'orch-parity: %s\n' "$1" >&2
  exit 4
}

git rev-parse --git-dir >/dev/null 2>&1 || fail "cwd ($(pwd)) is not inside a git repository"
ROOT="$(git rev-parse --show-toplevel)"
SOURCE_ENV="${ORCH_PARITY_ENV_FILE:-$ROOT/.env}"
[ -f "$SOURCE_ENV" ] || fail "no env file at $SOURCE_ENV — nothing to seed (set ORCH_PARITY_ENV_FILE)"
[ -x "$ROOT/scripts/orch_verify.sh" ] || fail "$ROOT/scripts/orch_verify.sh is missing or not executable"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/orch-env-parity.XXXXXX")"
TREE="$TMP/tree"
cleanup() {
  # --force: the seeded .env is intentionally untracked, and this tree is disposable. No branch
  # is created, so nothing of the operator's is deleted.
  git -C "$ROOT" worktree remove --force "$TREE" >/dev/null 2>&1 || true
  git -C "$ROOT" worktree prune >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

printf 'orch-parity: DEVELOPER CHECK (not the gate command)\n'
printf 'orch-parity: source checkout %s\n' "$ROOT"
printf 'orch-parity: seeded env file  %s (contents not logged)\n' "$SOURCE_ENV"
printf 'orch-parity: throwaway tree   %s\n' "$TREE"

git -C "$ROOT" worktree add --detach "$TREE" HEAD >/dev/null
cp "$SOURCE_ENV" "$TREE/.env"

cd "$TREE"
rc=0
./scripts/orch_verify.sh "$MODE" "${EXTRA_ARGS[@]}" || rc=$?
exit "$rc"