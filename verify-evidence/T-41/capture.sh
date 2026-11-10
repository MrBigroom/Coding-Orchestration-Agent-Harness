#!/usr/bin/env bash
# Regenerate verify-evidence/T-41/*.txt.
#
# Do NOT run this directly: run `orchestrator task evidence T-41` from a clean worktree. That
# driver refuses uncommitted inputs, executes this committed script, canonicalises the stamp on
# every file this pass rewrote to `# HEAD=<sha> mode=<fast|full>`, and writes manifest.json plus
# freshness_selfcheck.txt. The stamp names the code HEAD measured; the evidence-only commit that
# ships the bundle therefore does not stale it.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-41"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/t41-evidence.XXXXXX")"
SCRATCH=""
trap 'rm -rf "$STAGE" ${SCRATCH:+"$SCRATCH"}' EXIT
SHA="$(git rev-parse HEAD)"
MODE=fast
[ "${T15_FULL:-}" = "1" ] && MODE=full
STAMP="# HEAD=$SHA mode=$MODE"
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|FAILED|SKIPPED|ERROR|All checks|tsc_exit|listfiles_|^# (HEAD|command|exact|tsc|WORKAROUND|scratch|first|last)'

echo "capturing at $(git rev-parse --short HEAD) mode=$MODE"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/lint.txt"

{ echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test -vv tests/unit/test_sync_resilience.py tests/integration/test_sync_api.py tests/integration/test_cli.py -k sync"
  ./scripts/orch_verify.sh test -vv tests/unit/test_sync_resilience.py \
    tests/integration/test_sync_api.py tests/integration/test_cli.py -k sync; } 2>&1 \
  | grep -E "PASSED|FAILED|ERROR|passed|failed|^# (HEAD|command)|ORCH_VERIFY\|" \
  > "$STAGE/t41_tests.txt"

# TypeScript: this worktree has no web/node_modules, so a /tmp scratch project symlinks the
# MAIN checkout's node_modules and THIS worktree's web sources/configs. Nothing under the
# worktree is written. This proves the React sources typecheck; it is not a GUI runtime test.
COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN="${COMMON%/.git}"
{
  echo "$STAMP"
  echo "# WORKAROUND (disclosed): scratch project symlinks MAIN web/node_modules and THIS"
  echo "# worktree's web sources/configs; no file under the worktree is written."
  if [ ! -x "$MAIN/web/node_modules/.bin/tsc" ]; then
    echo "tsc_exit=2 (main checkout has no web/node_modules)"
  else
    SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/t41-tsc.XXXXXX")"
    ln -s "$MAIN/web/node_modules" "$SCRATCH/node_modules"
    for p in src index.html package.json tsconfig.json vite.config.ts \
             tailwind.config.js postcss.config.js; do
      ln -s "$ROOT/web/$p" "$SCRATCH/$p"
    done
    echo "# scratch src resolves to: $(readlink -f "$SCRATCH/src")"
    echo "# tsconfig used:           $(readlink -f "$SCRATCH/tsconfig.json")"
    echo "# exact command: $MAIN/web/node_modules/.bin/tsc -b --force (cwd=$SCRATCH)"
    cd "$SCRATCH"
    rc=0
    "$MAIN/web/node_modules/.bin/tsc" -b --force || rc=$?
    echo "tsc_exit=$rc"
    "$MAIN/web/node_modules/.bin/tsc" -p "$SCRATCH/tsconfig.json" --listFiles \
      > "$SCRATCH/listfiles.txt" 2>&1 || true
    echo "listfiles_total=$(wc -l < "$SCRATCH/listfiles.txt")"
    echo "listfiles_src=$(grep -c '/src/' "$SCRATCH/listfiles.txt" || true)"
    echo "# last listFiles line: $(tail -n 1 "$SCRATCH/listfiles.txt")"
    cd "$ROOT"
    if [ "$rc" -ne 0 ]; then exit "$rc"; fi
  fi
} > "$STAGE/tsc_web.txt" 2>&1

# Source anchors from the committed HEAD (line numbers are source lines, not this file's).
{
  echo "$STAMP"
  echo "# command: git show HEAD:<file> | grep -nE '<anchor>' (SQL block quoted verbatim)"
  for spec in \
    "src/orchestrator/services/sync_transport.py|class SyncPermanentError|class SyncTransientError|next_version = mapping.synced_version|p_version|async def remote_versions" \
    "src/orchestrator/services/sync_runner.py|def backoff_seconds|def _record_failure|identical repeats|def _notify_if_needed|health\[\"stopped\"\]|sync_failed|interval = float" \
    "src/orchestrator/services/sync_service.py|SYNC_HEALTH_KEY|def health|def reinitialise|conflicts_skipped|remote_ahead_skipped|version = synced" \
    "src/orchestrator/core/notification_vocab.py|sync_failed|Cloud sync needs attention" \
    "src/orchestrator/api/routes/sync.py|except SyncError|status_code = 503|SyncTransientError|sync_runner.status" \
    "src/orchestrator/cli.py|def sync_status|def sync_reinit|remote_versions|reinitialise" \
    "src/orchestrator/api/static_fallback/index.html|Cloud sync stopped|syncHint|next attempt" \
    "web/src/pages/Settings.tsx|notification_sync_failed|stopped_reason|consecutive_failures|Retrying with exponential backoff" \
    "docs/cloud-sync.md|Version protocol|invalid version transition|sync reinit|Log deduplication rule|retries indefinitely" ; do
    file="${spec%%|*}"; patterns="${spec#*|}"
    echo
    echo "## $file"
    git show "HEAD:$file" | grep -nE "$patterns" || true
  done
  echo
  echo "## supabase/migrations/0001_orchestrator_sync.sql:39-90 (the RPC contract, quoted)"
  git show "HEAD:supabase/migrations/0001_orchestrator_sync.sql" | sed -n '39,90p' \
    | nl -ba -v39 -w4 -s'  '
} > "$STAGE/source_anchors.txt"

cp "$STAGE"/*.txt "$OUT/"
echo "wrote: $(cd "$OUT" && ls *.txt | tr '\n' ' ')"
echo "# done at $(git rev-parse --short HEAD)"
