#!/usr/bin/env bash
# T-8 DoD 4/7 GUI evidence: the branch's TypeScript type-checks, and the recovery-commit
# surfaces are the ones the GUI renders. Runs `tsc -b` against THIS worktree's web/src by
# resolving the toolchain from the main checkout (a worktree has no node_modules — the same
# path-independence problem T-6 documented for Python), and proves which sources were read.
# No installs, no network. The scratch tree lives in /tmp so the worktree stays clean.
set -uo pipefail

WT=/home/mrbigroom/.local/share/coding-orchestrator/worktrees/coding-orchestrator/8
MAIN=/run/media/mrbigroom/Backup2TB/Projects/Coding-Orchestration-Agent-Harness
TMP=/tmp/t8-web-typecheck

rm -rf "$TMP"; mkdir -p "$TMP"
ln -s "$MAIN/web/node_modules" "$TMP/node_modules"
for f in src index.html package.json tsconfig.json vite.config.ts tailwind.config.js postcss.config.js; do
  ln -s "$WT/web/$f" "$TMP/$f"
done

echo "worktree HEAD   : $(git -C "$WT" rev-parse --short HEAD) ($(git -C "$WT" rev-parse --abbrev-ref HEAD))"
echo "main-repo HEAD  : $(git -C "$MAIN" rev-parse --short HEAD)"
echo "typecheck cwd   : $TMP"
echo "web/src resolves: $(readlink "$TMP/src")   # must be under the worktree"
echo

cd "$TMP" || exit 1
echo "== tsc -b --force (this branch's web/src) =="
./node_modules/.bin/tsc -b --force 2>&1 | tail -20
echo "TSC_EXIT=${PIPESTATUS[0]}"
echo
echo "== sources actually read (paths under $TMP/src, which symlink into the worktree) =="
./node_modules/.bin/tsc -p tsconfig.json --noEmit --listFiles 2>/dev/null \
  | grep -E "$TMP/src/(pages/TaskDetail|api/types)\.tsx?$" | sed "s|^|  |"
FILES=$(./node_modules/.bin/tsc -p tsconfig.json --noEmit --listFiles 2>/dev/null | grep -c "$TMP/src/")
echo "  $FILES source files type-checked, all resolved through the worktree symlink"
echo "  (tsc -p ... --noEmit exit: $(./node_modules/.bin/tsc -p tsconfig.json --noEmit >/dev/null 2>&1; echo $?))"
echo
echo "== the recovery surfaces the GUI renders =="
grep -n "RecoveryNote\|recovery_commits\|Badge value=\"recovery\"" "$WT/web/src/pages/TaskDetail.tsx" | sed 's/^/  /'
grep -n "RecoveryCommit" "$WT/web/src/api/types.ts" | sed 's/^/  /'
echo
echo "== baseline: main's own checkout type-checks the same way (so a red here would be ours) =="
cd "$MAIN/web" || exit 1
./node_modules/.bin/tsc -b --force 2>&1 | tail -5
echo "BASELINE_TSC_EXIT=${PIPESTATUS[0]}"

rm -rf "$TMP"
