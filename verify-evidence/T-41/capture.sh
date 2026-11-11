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
    "src/orchestrator/services/sync_transport.py|class SyncPermanentError|class SyncTransientError|RECOVERY_BY_KIND|def recovery_for|setdefault\(\"recovery\"|next_version = mapping.synced_version|p_version|async def remote_versions" \
    "src/orchestrator/services/sync_runner.py|def backoff_seconds|def _record_failure|identical repeats|def _notify_if_needed|claim_health_flag|complete_health_flag|release_health_flag|_next_notify_retry|sync_failed|health\[\"stopped\"\]|interval = float" \
    "src/orchestrator/services/sync_service.py|SYNC_HEALTH_KEY|HEALTH_FLAG_CLAIM_LEASE_SECONDS|def _read_health_raw|def health|def update_health|def write_health|def claim_health_flag|def complete_health_flag|def release_health_flag|compare-and-set|def reinitialise|conflicts_skipped|remote_ahead_skipped|version = synced" \
    "src/orchestrator/core/notification_vocab.py|sync_failed|Cloud sync needs attention" \
    "src/orchestrator/api/routes/sync.py|except SyncError|status_code = 503|SyncTransientError|sync_runner.status" \
    "src/orchestrator/cli.py|def sync_status|def sync_reinit|remote_versions|reinitialise" \
    "src/orchestrator/api/static_fallback/index.html|Cloud sync stopped|syncHint|next attempt" \
    "web/src/pages/Settings.tsx|notification_sync_failed|stopped_reason|consecutive_failures|Retrying with exponential backoff" \
    "docs/cloud-sync.md|Version protocol|invalid version transition|reconciled|sync reinit|Log deduplication rule|no single-writer assumption|retries indefinitely" ; do
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

# Live failure evidence (DoD 1): read-only measurement of the operator's log and device DB.
# The log grows while it is being read, so every count is taken against one frozen `head -n`
# snapshot and the snapshot's own sha256 is recorded. Nothing under ORCHESTRATOR_HOME is written.
LIVE_LOG="${T41_LIVE_LOG:-$HOME/.local/share/coding-orchestrator/logs/desktop-server.log}"
LIVE_DB="${T41_LIVE_DB:-$HOME/.local/share/coding-orchestrator/orchestrator.db}"
{
  echo "$STAMP"
  echo "# T-41 live measurement (read-only; no file under ORCHESTRATOR_HOME is modified)"
  echo "# measured_at=$(date -Iseconds)"
  echo "# log=$LIVE_LOG"
  if [ -r "$LIVE_LOG" ]; then
    N=$(wc -l < "$LIVE_LOG")
    head -n "$N" "$LIVE_LOG" > "$STAGE/live-snapshot.log"
    echo "live_lines=$N"
    echo "snapshot_sha256=$(sha256sum "$STAGE/live-snapshot.log" | cut -d' ' -f1)"
    echo "exact_0_3=$(grep -c 'invalid version transition: 0 -> 3' "$STAGE/live-snapshot.log")"
    echo "all_transition_lines=$(grep -c 'invalid version transition' "$STAGE/live-snapshot.log")"
    echo "background_failed_lines=$(grep -c 'background Supabase synchronization failed' "$STAGE/live-snapshot.log")"
    echo "### by transition pair (snapshot)"
    grep -oE 'invalid version transition: [0-9]+ -> [0-9]+' "$STAGE/live-snapshot.log" \
      | sort | uniq -c | sort -rn
    echo "### by terminal exception (snapshot)"
    grep -A30 'background Supabase synchronization failed' "$STAGE/live-snapshot.log" \
      | grep -oE '^(orchestrator\.[a-z_.]+\.)?[A-Za-z_][A-Za-z0-9_.]*Error:' \
      | sed -E 's/^orchestrator\.[a-z_.]+\.//' | sort | uniq -c | sort -rn
    # The reconciliation window is fixed: the operator's 7,373 figure was an all-transitions
    # count at this prefix, so `head -n 275740` reproduces it on any later copy of the file.
    head -n 275740 "$STAGE/live-snapshot.log" > "$STAGE/live-window.log"
    echo "### reconciliation window: head -n 275740 (the operator's snapshot)"
    echo "window_lines=$(wc -l < "$STAGE/live-window.log")"
    echo "window_all_transitions=$(grep -c 'invalid version transition' "$STAGE/live-window.log")"
    echo "window_exact_0_3=$(grep -c 'invalid version transition: 0 -> 3' "$STAGE/live-window.log")"
    echo "window_other_transitions=$(($(grep -c 'invalid version transition' "$STAGE/live-window.log") - $(grep -c 'invalid version transition: 0 -> 3' "$STAGE/live-window.log")))"
    echo "### by transition pair (reconciliation window)"
    grep -oE 'invalid version transition: [0-9]+ -> [0-9]+' "$STAGE/live-window.log" \
      | sort | uniq -c | sort -rn
  else
    echo "live_log=absent (not readable at $LIVE_LOG)"
  fi
  echo "# device-local DB=$LIVE_DB (sqlite mode=ro)"
  if [ -r "$LIVE_DB" ]; then
    "${PYTHON:-python3}" - "$LIVE_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
cur = con.cursor()
print("sync_state_keys=", [r[0] for r in cur.execute("select key from sync_state order by key")])
print("device_id_updated_at=", cur.execute("select updated_at from sync_state where key='device_id'").fetchone())
print("sync_entities_rows=", cur.execute("select count(*) from sync_entities").fetchone()[0])
print("sync_entities_dirty=", cur.execute("select count(*) from sync_entities where dirty=1").fetchone()[0])
print("sync_entities_conflicts=", cur.execute("select count(*) from sync_entities where conflict_json is not null and conflict_json not in ('null','')").fetchone()[0])
con.close()
PY
  else
    echo "live_db=absent (not readable at $LIVE_DB)"
  fi
} > "$STAGE/live_measurement.txt" 2>&1

cp "$STAGE"/*.txt "$OUT/"
echo "wrote: $(cd "$OUT" && ls *.txt | tr '\n' ' ')"
echo "# done at $(git rev-parse --short HEAD)"
