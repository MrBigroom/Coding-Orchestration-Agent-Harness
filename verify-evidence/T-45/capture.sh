#!/usr/bin/env bash
# Regenerate verify-evidence/T-45/*.txt.
#
# Do NOT run this directly: run `orchestrator task evidence T-45` from a clean worktree. That
# driver refuses uncommitted inputs, executes this committed script, canonicalises the stamp on
# every file this pass rewrote to `# HEAD=<sha> mode=<fast|full>`, and writes manifest.json plus
# freshness_selfcheck.txt. The stamp names the code HEAD measured; the evidence-only commit that
# ships the bundle therefore does not stale it.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="$ROOT/verify-evidence/T-45"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/t45-evidence.XXXXXX")"
SCRATCH=""
trap 'rm -rf "$STAGE" ${SCRATCH:+"$SCRATCH"}' EXIT
SHA="$(git rev-parse HEAD)"
MODE=fast
[ "${T15_FULL:-}" = "1" ] && MODE=full
STAMP="# HEAD=$SHA mode=$MODE"
KEEP='ORCH_VERIFY\||passed|failed|skipped|PASSED|FAILED|SKIPPED|ERROR|All checks|tsc_exit|listfiles_|^# (HEAD|command|exact|CONTROL|guards|checked)|^OK |^CONTROL|^source_command_count|^recovery_string_count|^python_command_count|^gui_command_count|^NOTIFICATION|^RECOVERY'

COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN="${COMMON%/.git}"
PY="$MAIN/.venv/bin/python"
[ -x "$PY" ] || PY=python3

echo "capturing at $(git rev-parse --short HEAD) mode=$MODE"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh test"
  ./scripts/orch_verify.sh test; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/full_suite.txt"

{ echo "$STAMP"; echo "# command: ./scripts/orch_verify.sh lint"
  ./scripts/orch_verify.sh lint; } 2>&1 | grep -E "$KEEP|^# (HEAD|command)" > "$STAGE/lint.txt"

{ echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test -vv tests/unit/test_operator_command_hints.py tests/unit/test_sync_resilience.py tests/integration/test_sync_api.py"
  ./scripts/orch_verify.sh test -vv tests/unit/test_operator_command_hints.py \
    tests/unit/test_sync_resilience.py tests/integration/test_sync_api.py; } 2>&1 \
  | grep -E "PASSED|FAILED|ERROR|passed|failed|^# (HEAD|command)|ORCH_VERIFY\|" \
  > "$STAGE/t45_tests.txt"

# Enumerate every operator-command hint the code can emit, using the same helpers the test uses,
# and parse each with the real Typer/Click tree (make_context; no callback runs). The scan covers
# every string expression in src/orchestrator (AST, including f-strings and concatenations), every
# command-shaped string in the two GUIs (raw text, not only <code>/mono spans), and resolves each
# flag against the command that owns it.
{
  echo "$STAMP"
  echo "# command: PYTHONPATH=$ROOT/src:$ROOT $PY - (test helpers drive the CLI parser)"
  PYTHONPATH="$ROOT/src:$ROOT" "$PY" - <<'PY'
from tests.unit.test_operator_command_hints import (
    _assert_commands_are_real,
    _audit_gui_files,
    _audit_python_tree,
    _audit_text,
    _emitted_recovery_strings,
    _parse_cli,
)
from orchestrator.core.notification_vocab import TRIGGERS

recovery = _emitted_recovery_strings()
print(f"recovery_string_count={len(recovery)}")
for label, text in recovery.items():
    print(f"RECOVERY {label}: {text}")
    audit = _audit_text(text)
    _assert_commands_are_real(audit)
    for command in audit:
        _parse_cli(command)
        print(f"OK recovery command: {command}")

body = TRIGGERS["sync_failed"].action
print(f"NOTIFICATION sync_failed: {body}")
assert "conflicts_skipped" in body and "left untouched" in body
_assert_commands_are_real(_audit_text(body))
print("OK sync_failed body names the skip behaviour and parses")

python_audit = _audit_python_tree()
print(f"python_command_count={len(python_audit)}")
for command in sorted(python_audit):
    flags = sorted(python_audit[command]["flags"])
    sources = sorted(python_audit[command]["sources"])
    print(f"OK python command: {command} flags={flags} sources={len(sources)}")
    if len(sources) <= 4:
        for source in sources:
            print(f"    at {source}")
_assert_commands_are_real(python_audit)

gui_audit = _audit_gui_files()
print(f"gui_command_count={len(gui_audit)}")
for command in sorted(gui_audit):
    print(
        f"OK gui command: {command}"
        f" flags={sorted(gui_audit[command]['flags'])}"
        f" sources={sorted(gui_audit[command]['sources'])}"
    )
_assert_commands_are_real(gui_audit)

# Control 1: the shipped hint must be rejected by this same validator.
try:
    _parse_cli("orchestrator sync reinit --dry-run")
except Exception as exc:
    print(f"CONTROL rejected the shipped hint: {type(exc).__name__}: {exc}")
else:
    raise SystemExit("CONTROL FAILED: the validator accepted the shipped --dry-run hint")

# Control 2: a wrong flag in its own backticks/span must be caught, not only a wrong command.
bad = "run `orchestrator sync reinit` (the dry run is the default and `--nope` writes)"
try:
    _assert_commands_are_real(_audit_text(bad))
except AssertionError as exc:
    print(f"CONTROL rejected a separately formatted bogus flag: {exc}")
else:
    raise SystemExit("CONTROL FAILED: a flag in its own backticks escaped the audit")
PY
} > "$STAGE/command_audit.txt" 2>&1

# The real CLI, not the parser helper: the old hint is a usage error and the corrected forms are
# accepted. --help is appended so an accepted form exits without touching the network.
{
  echo "$STAMP"
  echo "# command: PYTHONPATH=$ROOT/src $MAIN/.venv/bin/orchestrator sync reinit --dry-run --help"
  set +e
  PYTHONPATH="$ROOT/src" "$MAIN/.venv/bin/orchestrator" sync reinit --dry-run --help \
    > "$STAGE/old-hint.out" 2>&1
  echo "old_hint_exit=$?"
  grep -E "No such option: --dry-run" "$STAGE/old-hint.out" || true
  echo "# command: PYTHONPATH=$ROOT/src $MAIN/.venv/bin/orchestrator sync reinit --help"
  PYTHONPATH="$ROOT/src" "$MAIN/.venv/bin/orchestrator" sync reinit --help \
    > "$STAGE/new-hint.out" 2>&1
  echo "corrected_default_exit=$?"
  grep -E "Dry run by default|--apply" "$STAGE/new-hint.out" | sed 's/[│╭╮╰╯─]//g' || true
  echo "# command: PYTHONPATH=$ROOT/src $MAIN/.venv/bin/orchestrator sync reinit --apply --help"
  PYTHONPATH="$ROOT/src" "$MAIN/.venv/bin/orchestrator" sync reinit --apply --help \
    > "$STAGE/apply-hint.out" 2>&1
  echo "corrected_apply_exit=$?"
  set -e
  echo "# guards: no operator hint may keep --dry-run on the reinit command"
  # Exclude tests on purpose: the T-45 test file quotes the broken string to prove it is
  # rejected, so only operator-facing files are guarded here.
  if git grep -n -- 'sync reinit --dry-run' -- . ':(exclude)verify-evidence' ':(exclude)tests' ; then
    echo "GUARD FAILED: a stale reinit --dry-run hint is still in the tree"
  else
    echo "guards: no 'sync reinit --dry-run' in operator-facing files (tests quote it on purpose)"
  fi
  echo "# guards: every remaining --dry-run mention outside evidence"
  git grep -n -- '--dry-run' -- . ':(exclude)verify-evidence' | sed 's/^/  /' || true
} > "$STAGE/live_cli.txt" 2>&1

# TypeScript: this worktree has no web/node_modules, so a /tmp scratch project symlinks the
# MAIN checkout's node_modules and THIS worktree's web sources/configs. Nothing under the
# worktree is written. This proves Settings.tsx typechecks; it is not a GUI runtime test.
{
  echo "$STAMP"
  echo "# WORKAROUND (disclosed): scratch project symlinks MAIN web/node_modules and THIS"
  echo "# worktree's web sources/configs; no file under the worktree is written."
  if [ ! -x "$MAIN/web/node_modules/.bin/tsc" ]; then
    echo "tsc_exit=2 (main checkout has no web/node_modules)"
  else
    SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/t45-tsc.XXXXXX")"
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
    echo "# Settings.tsx in listFiles: $(grep -c '/src/pages/Settings.tsx' "$SCRATCH/listfiles.txt" || true)"
    cd "$ROOT"
    if [ "$rc" -ne 0 ]; then exit "$rc"; fi
  fi
} > "$STAGE/tsc_web.txt" 2>&1

# Source anchors from the committed HEAD (line numbers are source lines, not this file's).
{
  echo "$STAMP"
  echo "# command: git show HEAD:<file> | grep -nE '<anchor>'"
  for spec in \
    "src/orchestrator/services/sync_transport.py|VERSION_REJECTION_RECOVERY|RECOVERY_BY_KIND|def recovery_for|invalid version transition|detail\[\"recovery\"\] = VERSION_REJECTION_RECOVERY" \
    "src/orchestrator/core/notification_vocab.py|sync_failed|orchestrator sync reinit|conflicts_skipped|left untouched" \
    "src/orchestrator/api/static_fallback/index.html|sync reinit|conflicts_skipped|--dry-run|dry run by default" \
    "web/src/pages/Settings.tsx|sync reinit|dry run by default|conflicts_skipped|--dry-run" \
    "docs/cloud-sync.md|sync reinit|dry run is the default|conflicts_skipped" \
    "docs/decisions.md|sync reinit|dry run by default" \
    "README.md|sync reinit|dry run by default|conflicts_skipped" \
    "tests/unit/test_operator_command_hints.py|def test_every_recovery_string|def test_sync_failed_notification|def test_the_validator_rejects|def test_a_flag_in_its_own_backticks|def test_every_command_shaped_string|def test_every_command_shaped_string_in_the_guis" \
    "tests/integration/test_sync_api.py|VERSION_REJECTION_RECOVERY" \
    "tests/unit/test_sync_resilience.py|VERSION_REJECTION_RECOVERY" ; do
    file="${spec%%|*}"; patterns="${spec#*|}"
    echo
    echo "## $file"
    git show "HEAD:$file" | grep -nE "$patterns" || true
  done
} > "$STAGE/source_anchors.txt"

cp "$STAGE"/*.txt "$OUT/"
echo "wrote: $(cd "$OUT" && ls *.txt | tr '\n' ' ')"
echo "# done at $(git rev-parse --short HEAD)"
