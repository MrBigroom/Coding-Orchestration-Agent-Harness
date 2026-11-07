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
# `<tmpdir>/orch-env-parity.XXXXXX` and holds a `.lock` and a `.repo` marker (the checkout it
# belongs to). `orchestrator cleanup` (and the doctor parity probe) reclaims roots whose lock is
# free, and never touches one a live run holds or one that is not a real root this harness created.
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
# parity run is using. The FILE is created before the worktree so the root carries it from the
# start, but the root is not owned by this run until `flock` below succeeds -- see the window note.
# `.repo` records which checkout this root belongs to, so another project's probe ignores it
# instead of reporting a busy lock that is none of its business.
#
# This order is NOT a claim that the root is always locked: between `mktemp -d` and `flock` there
# is a window where the root exists with no lock and no `.repo`. The sweeper closes that window
# itself (T-20 round 5): an unmarked root younger than a 60-second grace is reported as too young
# to judge and never swept; only an older unmarked root is treated as a pre-T-20 orphan.
LOCK="$TMP/.lock"
# Open WITHOUT truncating (`>>`), so a lock file the sweeper already created keeps its pid until
# this run acquires the lock and records its own. Install the cleanup trap BEFORE any failure can
# exit, so a failure before the worktree exists does not leave the new root behind either.
exec 9>>"$LOCK"
cleanup() {
  # --force: the seeded .env is intentionally untracked, and this tree is disposable. No branch
  # is created, so nothing of the operator's is deleted.
  git -C "$ROOT" worktree remove --force "$TREE" >/dev/null 2>&1 || true
  git -C "$ROOT" worktree prune >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM
if ! flock -n 9; then
  fail "another parity run is using $TMP"
fi
# Record the holder so a sweep may only break this lock once `os.kill(pid, 0)` says it is gone.
# Truncate BY PATH now that the flock is ours: the same inode, so fd 9's lock is untouched.
# Appending (`>&9`) left every previous holder's pid in the file while the Python reader takes the
# FIRST line, so one stale pid could keep a genuinely orphaned lock unbreakable forever (T-20
# review round 5).
printf '%s\n' "$$" > "$LOCK"
printf '%s\n' "$ROOT" > "$TMP/.repo"

printf 'orch-parity: DEVELOPER CHECK (not the gate command)\n'
printf 'orch-parity: source checkout %s\n' "$ROOT"
printf 'orch-parity: env variable names from %s (values are never copied)\n' "$SOURCE_ENV"
printf 'orch-parity: throwaway tree   %s\n' "$TREE"

git -C "$ROOT" worktree add --detach "$TREE" HEAD >/dev/null

# Placeholder for one variable NAME. Never derived from the value in the source file. The
# classification is by substring and MUST agree with project_service._synthetic_env_value: the two
# parity paths write the same seed for the same name list.
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
# written into the worktree.
# Sorted under LC_ALL=C so a re-run writes the same file AND the sequence matches Python's
# `sorted()` in project_service._env_variable_names byte for byte (T-20 review round 3); the
# per-name placeholder below is compared against the Python path by
# tests/integration/test_env_hermeticity.py.
names="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\2/p' "$SOURCE_ENV" | LC_ALL=C sort -u)"
# A tracked `.env` in the throwaway tree is never overwritten (the Python path is create-only for
# the same reason); `noclobber` below is the race-safe backstop for this pre-check.
[ ! -e "$TREE/.env" ] || fail "$TREE/.env already exists; refusing to overwrite it"
(
  # `umask 077` must be in force when the file is CREATED, so the redirect lives INSIDE this
  # subshell. Attaching `> ...` to the group from outside would have the PARENT shell open it at
  # the parent umask (typically 0644) and chmod afterwards, which is not the T-19 property the
  # docs claim. `noclobber` makes the create exclusive, matching the Python `O_EXCL` path.
  umask 077
  set -o noclobber
  exec > "$TREE/.env"
  printf '# Synthetic parity environment: variable NAMES from the operator env file.\n'
  printf '# Values are placeholders; no credential value is copied (T-20).\n'
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    printf '%s=%s\n' "$name" "$(parity_value "$name")"
  done <<< "$names"
)

cd "$TREE"
rc=0
./scripts/orch_verify.sh "$MODE" "${EXTRA_ARGS[@]}" || rc=$?
exit "$rc"
