#!/usr/bin/env bash
# orch_env_parity.sh — DEVELOPER parity check, never the gate command.
#
# The gate runs commands in a tracked-files-only worktree: no `.env`, node_modules or `.venv`.
# That is deliberate (T-19 records it in verification.json), but it means a gate green and a red
# run in your checkout can both be true. This asks the other question: does the suite still pass
# with my local `.env` present? It creates a THROWAWAY detached worktree of the current HEAD,
# derives a SYNTHETIC `.env` from the variable NAMES in the local file (never its values), and runs
# the same path-independent wrapper the gate uses. It is never part of `make test`/`make verify`.
#
# T-20: no credential value is ever written into the harness-managed tree. The throwaway root is
# `<tmpdir>/orch-env-parity.XXXXXX` and holds a `.lock`; `orchestrator cleanup` (and the doctor
# parity probe) reclaims roots whose lock is free, and never touches one a live run holds.
#
# Usage: scripts/orch_env_parity.sh [test|lint|all] [wrapper args...]
#   Default mode is `test`; extra args go to scripts/orch_verify.sh.
#   ORCH_PARITY_ENV_FILE   env file to seed (default <repo root>/.env)
#
# Exit codes: 0 parity run green · 4 no env file to seed / not a repository / lock busy ·
# otherwise the wrapper's own exit code.
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
# The lock is what lets a sweep tell an orphaned tree (lock free after SIGKILL) from one another
# parity run is using. It is created before the worktree so the root is never unowned.
LOCK="$TMP/.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  fail "another parity run is using $TMP"
fi
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
printf 'orch-parity: env variable names from %s (values are never copied)\n' "$SOURCE_ENV"
printf 'orch-parity: throwaway tree   %s\n' "$TREE"

git -C "$ROOT" worktree add --detach "$TREE" HEAD >/dev/null

# Placeholder for one variable NAME. Never derived from the value in the source file.
parity_value() {
  local upper="${1^^}"
  case "$upper" in
    *URL*|*URI*|*ENDPOINT*) printf 'https://synthetic.invalid' ;;
    *EMAIL*) printf 'synthetic@example.invalid' ;;
    *KEY*|*SECRET*|*TOKEN*|*PASSWORD*|*PASSWD*) printf 'synthetic-not-a-secret' ;;
    *) printf 'synthetic' ;;
  esac
}

# Synthesise the parity environment from NAMES only (T-20 DoD 1). The sed expression emits the
# name before the first `=`; values never leave the source file, are never printed, and are never
# written into the worktree. 0600 keeps the synthetic file from being world-readable anyway.
names="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\2/p' "$SOURCE_ENV" | sort -u)"
(
  umask 077
  printf '# Synthetic parity environment: variable NAMES from the operator env file.\n'
  printf '# Values are placeholders; no credential value is copied (T-20).\n'
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    printf '%s=%s\n' "$name" "$(parity_value "$name")"
  done <<< "$names"
) > "$TREE/.env"
chmod 600 "$TREE/.env"

cd "$TREE"
rc=0
./scripts/orch_verify.sh "$MODE" "${EXTRA_ARGS[@]}" || rc=$?
exit "$rc"
