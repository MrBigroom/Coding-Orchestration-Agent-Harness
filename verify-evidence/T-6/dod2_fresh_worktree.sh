#!/usr/bin/env bash
# T-6 DoD 2 proof: run the COMMITTED wrapper in a FRESH `git worktree add` of this repo.
#
# It does both readings of DoD 2 in one script:
#   (a) LITERAL: `git worktree add --detach <tmp> main`. Today that worktree has NO
#       scripts/orch_verify.sh (the wrapper is created by this branch), and it shows the
#       trap: the main-checkout venv interpreter imports the MAIN checkout.
#   (b) AS-IF-MERGED: the same worktree then takes this branch's tracked content
#       (`git checkout <SHA> -- .`), which is byte-identical to what post-merge `main`
#       will hold (the merge is a fast-forward: main...HEAD is `0 N` and
#       `git merge-tree --write-tree main HEAD` returns HEAD's own tree). From there the
#       wrapper runs THIS worktree's tests against THIS worktree's src.
#
# No network, no API keys, nothing installed. The throwaway worktree is always removed.
set -uo pipefail
MAIN=/run/media/mrbigroom/Backup2TB/Projects/Coding-Orchestration-Agent-Harness
WT=$(git rev-parse --show-toplevel)
SHA=$(git rev-parse HEAD)
P=/tmp/t6-dod2-proof
OUT=${1:-$P.out}
exec >"$OUT" 2>&1

cleanup() {
  cd / || true
  git -C "$MAIN" worktree remove --force "$P" 2>/dev/null
  git -C "$MAIN" worktree prune 2>/dev/null
}
trap cleanup EXIT

echo "== commit under test: $(git rev-parse --abbrev-ref HEAD) @ $SHA =="
echo
echo "== 0. main...HEAD (behind ahead). '0 N' means merging this branch is a fast-forward =="
printf '   '; git rev-list --left-right --count main...HEAD
echo "   main tree  : $(git rev-parse main^{tree})"
echo "   HEAD tree  : $(git rev-parse HEAD^{tree})"
echo "   merge tree : $(git merge-tree --write-tree main "$SHA" 2>/dev/null | head -1)"
echo "   wrapper on main? $(git cat-file -e main:scripts/orch_verify.sh 2>/dev/null && echo yes || echo 'NO (branch creates it)')"

echo
echo "== 1. LITERAL: git worktree add --detach $P main  (tracked files only) =="
git -C "$MAIN" worktree remove --force "$P" 2>/dev/null
git -C "$MAIN" worktree prune 2>/dev/null
rm -rf "$P"
git -C "$MAIN" worktree add --detach "$P" main >/dev/null || { echo "   worktree add FAILED"; exit 1; }
cd "$P" || exit 1
echo "   worktree path      : $P"
echo "   own .venv?         $( [ -e "$P/.venv" ] && echo YES || echo 'NO  <- the T-6 bug: a worktree gets tracked files only')"
echo "   wrapper present?   $( [ -f "$P/scripts/orch_verify.sh" ] && echo yes || echo 'NO  <- wrapper ships with this branch, so DoD 2 cannot be run literally against main until merge')"
echo "   base content       : $(git rev-parse --short HEAD)"

echo
echo "== 2. THE TRAP, in that fresh worktree of main: the venv interpreter imports the MAIN checkout =="
export VIRTUAL_ENV="$MAIN/.venv"   # exactly what the server child process inherits today
echo "   inherited VIRTUAL_ENV = $VIRTUAL_ENV"
"$MAIN/.venv/bin/python" -c 'import orchestrator; print("   unwrapped import ->", orchestrator.__file__)'

echo
echo "== 3. AS-IF-MERGED: the same worktree detached at this branch's commit =="
# main...HEAD is `0 N` and merge-tree returns HEAD's own tree, so this content is exactly
# what post-merge `main` will hold. We move HEAD itself (not just the worktree files) because
# `git ls-tree HEAD` based assertions must see the branch tree too.
git -C "$P" checkout --detach "$SHA" >/dev/null 2>&1 || { echo "   checkout --detach FAILED"; exit 1; }
echo "   content now  : $(git -C "$P" rev-parse --short HEAD) (detached, == post-merge main tree)"
echo "   wrapper present + executable? $( [ -x "$P/scripts/orch_verify.sh" ] && echo 'yes (exec bit kept)' || echo NO )"
cd "$P" || exit 1

echo
echo "== 4. WRAPPED resolve: the assertion names THIS worktree =="
./scripts/orch_verify.sh resolve
echo "   resolve exit=$?"

echo
echo "== 5. GUARD: a module that resolves outside the worktree trips exit 3 and runs nothing =="
ORCH_VERIFY_PACKAGE=email ./scripts/orch_verify.sh resolve
echo "   guard exit=$? (3 = resolved OUTSIDE the worktree)"
echo
echo "== 5b. GUARD: an unresolvable module also fails closed =="
ORCH_VERIFY_PACKAGE=nope_not_a_module ./scripts/orch_verify.sh resolve
echo "   guard exit=$? (3 = could not resolve)"

echo
echo "== 6. WRAPPED single-file pytest run: this worktree's tests, this worktree's src =="
./scripts/orch_verify.sh test tests/integration/test_verify_wrapper.py
echo "   test exit=$?"

echo
echo "== 7. WRAPPED lint run in the fresh worktree =="
./scripts/orch_verify.sh lint
echo "   lint exit=$?"

echo
echo "== 8. WRAPPED FULL suite in the fresh worktree (DoD 5: passes via the wrapper itself) =="
./scripts/orch_verify.sh test
echo "   full-suite exit=$?"

echo
echo "== 9. cleaned up? =="
git -C "$MAIN" worktree list
echo "DONE dod2"
