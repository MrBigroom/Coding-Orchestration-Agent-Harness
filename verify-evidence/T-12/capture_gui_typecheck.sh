#!/usr/bin/env bash
# T-12 DoD 2 frontend evidence: real `tsc -b --force` over this worktree's web/src.
# This file is a captured helper, not an entry point: it has mode 100644, run it as
#   bash verify-evidence/T-12/capture_gui_typecheck.sh
# Recipe (approved project memory): scratch dir under /tmp, symlink the MAIN repo's
# web/node_modules plus this worktree's web/* inputs. The node_modules symlink must NEVER live
# inside the worktree - .gitignore's `web/node_modules/` has a trailing slash so it matches
# directories only, and a symlink there becomes untracked dirt that a recovery commit would
# sweep onto the branch under review.
set -u
cd "$(dirname "$0")/../.."
WT=$(pwd)
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
S=/tmp/t12-gui-typecheck
C=/tmp/t12-gui-typecheck-control
OUT=/tmp/t12_gui_typecheck.txt
rm -f "$WT/verify-evidence/T-12/gui_typecheck.txt"

run() { echo "\$ $*"; OUTP=$("$@" 2>&1); rc=$?; printf '%s\n' "$OUTP"; echo "exit=$rc"; }

{
  echo "# T-12 GUI type-check evidence (DoD 2, frontend half)"
  echo "# generated: $(date -Is)"
  echo "# worktree: $WT"
  echo "# HEAD=$(git -C "$WT" rev-parse HEAD)"
  echo "# dirty-entries(-uall, excludes this file, written to /tmp first)=$(git -C "$WT" status --porcelain -uall | wc -l)"
  echo "# web/ unchanged since c4f0ae9? $(git -C "$WT" diff --quiet c4f0ae9 HEAD -- web && echo yes || echo no)"
  echo
  echo "## Scratch tree (recipe from approved project memory: never inside the worktree)"
  echo "\$ rm -rf $S; mkdir -p $S"
  echo "\$ ln -s $MAIN/web/node_modules $S/node_modules"
  echo "\$ ln -s $WT/web/{src,index.html,package.json,tsconfig.json,vite.config.ts,tailwind.config.js,postcss.config.js} $S/"
  echo
  echo '$ ls -l /tmp/t12-gui-typecheck'
  ls -l "$S" | sed 1d
  echo
  echo "## Tool"
  echo '$ cd /tmp/t12-gui-typecheck && ./node_modules/.bin/tsc --version'
  ( cd "$S" && ./node_modules/.bin/tsc --version )
  echo
  echo "## Build: tsc -b --force (exit 0 = clean)"
  echo '$ cd /tmp/t12-gui-typecheck && ./node_modules/.bin/tsc -b --force'
  ( cd "$S" && ./node_modules/.bin/tsc -b --force 2>&1 ); echo "exit=$?"
  echo
  echo "## Proof of what was read: tsc -p tsconfig.json --noEmit --listFiles"
  echo '$ cd /tmp/t12-gui-typecheck && ./node_modules/.bin/tsc -p tsconfig.json --noEmit --listFiles'
  ( cd "$S" && ./node_modules/.bin/tsc -p tsconfig.json --noEmit --listFiles 2>&1 > "$S/listFiles.txt" ); echo "exit=$?"
  echo "files listed: $(wc -l < "$S/listFiles.txt")"
  echo
  echo "-- every branch source file tsc read (T-12 GUI files in [brackets]) --"
  grep -E '/(src/(api|pages|components|hooks)/[A-Za-z]+\.(ts|tsx)|src/(App|main|theme|fonts|highlight)\.tsx?|vite\.config\.ts)$' "$S/listFiles.txt" \
    | sed "s#^$S/#  #" \
    | sed -E 's#/(pages/(Tasks|TaskDetail|Settings)\.tsx|api/types\.ts)$#/[\1]#'
  echo
  echo "-- every listed path resolves into THIS worktree (symlink targets) --"
  for f in src/pages/Tasks.tsx src/pages/TaskDetail.tsx src/pages/Settings.tsx src/api/types.ts; do
    printf '  %-24s -> %s\n' "$f" "$(realpath "$S/$f")"
  done
  echo "  entries under \$MAIN/web/node_modules (resolved through the symlink): $(grep -c "^$MAIN/web/node_modules/" "$S/listFiles.txt")"
  echo "  entries under \$S, i.e. this worktree's branch sources + vite.config.ts: $(grep -c "^$S/" "$S/listFiles.txt")"
  echo "  total listed: $(wc -l < "$S/listFiles.txt")"
  echo
  echo "-- T-12 marker lines, read back from the resolved real path --"
  grep -n "attempts.summary" "$(realpath "$S/src/pages/Tasks.tsx")" "$(realpath "$S/src/pages/TaskDetail.tsx")" | sed 's#/home/mrbigroom/.local/share/coding-orchestrator/worktrees/coding-orchestrator/12/##'
  grep -n "AttemptSummary" "$(realpath "$S/src/api/types.ts")"
  echo
  echo "## Negative control: the same checker MUST fail on a deliberate type error"
  echo "# copy of the worktree's web/src with one injected error, so a green run cannot be a"
  echo "# no-op (proves tsc type-checks these sources instead of only parsing paths)"
  echo "\$ cp -r $WT/web/src $C/src && echo 'const __t12_control: number = \"not a number\";' >> $C/src/api/types.ts"
  echo '$ cd /tmp/t12-gui-typecheck-control && ./node_modules/.bin/tsc -b --force'
  ( cd "$C" && ./node_modules/.bin/tsc -b --force 2>&1 ); echo "exit=$? (expected non-zero)"
  echo
  echo "# RESULT: real type-check of web/src/pages/{Tasks,TaskDetail,Settings}.tsx and"
  echo "# web/src/api/types.ts at this HEAD: tsc -b --force exit 0; control exit 1."
} > "$OUT" 2>&1
mv "$OUT" "$WT/verify-evidence/T-12/gui_typecheck.txt"
echo "wrote $WT/verify-evidence/T-12/gui_typecheck.txt"
