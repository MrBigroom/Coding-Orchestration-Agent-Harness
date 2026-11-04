#!/usr/bin/env bash
# T-4 evidence capture: one invocation, one stamped HEAD. Outputs are staged in /tmp and moved
# into verify-evidence/T-4 only after every command ran, so the dirty-entries stamp is real.
# Plain committed UTF-8 text; nothing here is hidden from `git diff`. `.log` is gitignored in
# this repo, so the files are named .txt.
#
# Re-run: bash verify-evidence/T-4/capture.sh
set -uo pipefail
cd "$(dirname "$0")/../.."

WT=$(pwd)
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
HEAD=$(git rev-parse HEAD)
STAMP="# HEAD=$HEAD ($(git rev-parse --short HEAD)) branch=$(git branch --show-current) captured=$(date -Is) tree=$(git status --porcelain | wc -l) dirty-entries"
TMP=$(mktemp -d /tmp/t4-evidence.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

echo "$STAMP"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh test   (whole tests/ tree of THIS worktree)"
  ./scripts/orch_verify.sh test
  echo "SUITE_EXIT=$?"
} >"$TMP/full_suite.txt"

{
  echo "$STAMP"
  # pytest addopts is "-q", so a single -v cancels it: -vv is needed to name each test.
  echo "# command: ./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py"
  echo "#          tests/integration/test_memory_scope_boundary.py -vv"
  ./scripts/orch_verify.sh test tests/unit/test_memory_scopes.py \
    tests/integration/test_memory_scope_boundary.py -vv
  echo "TARGETED_EXIT=$?"
} >"$TMP/memory_scope_tests.txt"

{
  echo "$STAMP"
  echo "# command: ./scripts/orch_verify.sh lint   (ruff check src tests)"
  ./scripts/orch_verify.sh lint
  echo "LINT_EXIT=$?"
  echo
  echo "# Cold, offline demonstration against THIS worktree's code, in a throwaway"
  echo "# ORCHESTRATOR_HOME: coercion with the substitution recorded, boundary rejection for"
  echo "# all three categories, the repair re-rendering BOTH files and collecting the orphan,"
  echo "# the list marker, and the CLI verbatim."
  echo "# command: ORCHESTRATOR_HOME=\$TMP/home PYTHONPATH=\$WT/src \$MAIN/.venv/bin/python <demo>"
  env -u VIRTUAL_ENV -u ORCHESTRATOR_ENABLE_WORKER -u ORCHESTRATOR_ENABLE_SCHEDULER \
    ORCHESTRATOR_HOME="$TMP/home" PYTHONPATH="$WT/src" \
    "$MAIN/.venv/bin/python" - <<'PY'
import os
from pathlib import Path

from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.core import db
from orchestrator.core.models import MemoryEntry
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion
from orchestrator.services import bootstrap
from orchestrator.services import memory_service as m

SLUG = "ai-livestreaming-avatar"
HOME = Path(os.environ["ORCHESTRATOR_HOME"])


def rel(p) -> str:
    return str(Path(str(p)).relative_to(HOME))


print("vocabularies:", {"global": m.GLOBAL_SCOPES, "project": m.PROJECT_SCOPES,
                        "role": m.ROLE_SCOPES, "aliases": m.ROLE_SCOPE_ALIASES})
print("fallbacks   :", dict(m.FALLBACK_SCOPE))
bootstrap.init()

print("\n(1) A WORKER HANDOFF'S SUGGESTIONS  (DoD 1: coerce project/global, drop role)")
with db.session_scope() as s:
    out = m.suggest_from_handoff_report(
        s,
        [
            MemorySuggestion(
                category="project", scope=SLUG,
                content="The repo is reachable directly on this host; the /sessions/<id>/mnt "
                        "mount named in briefs does not exist."),
            MemorySuggestion(
                category="global",
                scope="global-environment-verifying-orchestrator-change",
                content="Verification runs inside the task worktree."),
            MemorySuggestion(category="role", scope=f"{SLUG}-logbook",
                             content="The logbook writer keeps a running diary."),
        ],
        "handoff:R-3", SLUG,
    )
    print("  stored:", [(e.id, e.category, e.scope, "pending" if not e.approved else "approved")
                        for e in out.created])
    print("  recorded notes:")
    for n in out.notes:
        print("    -", n)

print("\n(2) BOUNDARY REJECTION, EVERY CATEGORY  (DoD 2)")
with db.session_scope() as s:
    for kwargs in (
        {"category": "global", "scope": "environment-notes", "content": "x"},
        {"category": "project", "scope": "logbook", "project_slug": "demo", "content": "x"},
        {"category": "role", "scope": "logbook-writer", "content": "x"},
    ):
        try:
            e = m.create(s, MemoryCreate(**kwargs))
            print("  NOT REJECTED:", e.category, e.scope)
        except m.MemoryError_ as err:
            print("  rejected:", err)
    print("  accepted, alias resolved through ROLE_MEMORY_KEY:",
          m.create(s, MemoryCreate(category="role", scope="reviewer",
                                   content="Gate the merge.")).scope)

print("\n(3) REPAIR OF ROWS STORED BEFORE THE CHECK  (DoD 3)")
legacy_ids = []
with db.session_scope() as s:
    for kwargs in (
        {"category": "global", "scope": "global-environment-verifying-orchestrator",
         "content": "Verification is path-independent.", "approved": True},
        {"category": "project", "scope": SLUG, "project_slug": SLUG,
         "content": "Repo is reachable.", "approved": True},
        {"category": "project", "scope": SLUG, "project_slug": "p2",
         "content": "Rejected, so untouched.", "rejected": True},
    ):
        row = MemoryEntry(source="handoff:R-3", role=None, **kwargs)
        s.add(row)
        s.commit()
        s.refresh(row)
        if row.approved:
            m.render_file(s, m._file_for(row))
        legacy_ids.append(row.id)
    root = m.memory_root()
    print("  legacy ids:", legacy_ids)
    print("  files before   :", sorted(str(p.relative_to(root)) for p in root.rglob("*.md")))
    print("  orphan files   :", sorted(str(p.relative_to(root)) for p in m.orphan_files(s)))
    print("  uninjectable   :", [e.id for e in m.list_entries(s)
                                 if not m.is_injectable(e)])
    dry = m.fix_scopes(s, dry_run=True)
    print("  dry run would move:", [(x["id"], x["from"], x["to"], x["collect_old_file"])
                                    for x in dry.moved])
    print("  dry run left     :", [(e.id, e.scope) for e in m.list_entries(s)
                                    if e.id in legacy_ids])
    rep = m.fix_scopes(s)
    print("  moved ids        :", rep.moved_ids)
    for mv in rep.moved:
        print("    #%s %s %s -> %s | old_file %s | new_file %s" % (
            mv["id"], mv["category"], mv["from"], mv["to"],
            mv["old_file"].replace(str(root) + "/", ""),
            mv["new_file"].replace(str(root) + "/", "")))
    print("  left alone       :", [(x["id"], x["why"]) for x in rep.skipped])
    print("  collected files  :", [str(p.relative_to(root)) for p in rep.removed_files])
    print("  files after      :", sorted(str(p.relative_to(root)) for p in root.rglob("*.md")))
    for name in ("global/environment.md", f"projects/{SLUG}/gotchas.md"):
        lines = [ln for ln in (root / name).read_text().splitlines() if ln.startswith("- ")]
        print(f"  {name} now holds:", lines)
    print("  rows now         :", [(e.id, e.category, e.scope, e.approved, e.rejected)
                                   for e in m.list_entries(s) if e.id in legacy_ids])
    again = m.fix_scopes(s)
    print("  second run (idempotent): moved", again.moved_ids, "skipped",
          [x["id"] for x in again.skipped], "collected", len(again.removed_files))

print("\n(4) THE LIST MARKER, INCLUDING THE COUNTER-EXAMPLES  (DoD 4)")
with db.session_scope() as s:
    bad = MemoryEntry(category="project", scope=SLUG, project_slug=SLUG, content="Orphan.",
                      source="handoff:R-3", approved=True)
    s.add(bad)
    s.commit()
    s.refresh(bad)
    for e in [bad] + [
        MemoryEntry(id=900 + i, category="role", scope=scope, role=role, content="valid")
        for i, (scope, role) in enumerate((
            ("worker", None), ("worker", "worker"), ("architect", "reviewer"),
            ("agent", "agent"), ("supervisor", "supervisor"),
        ))
    ]:
        ok, reason = m.injectability(e)
        print("  #%s %s scope=%r role=%r -> %s  reachable_by=%s %s" % (
            e.id, e.category, e.scope, e.role, "ok" if ok else "NO",
            ",".join(m.reachable_roles(e)) or "-", reason))

print("\n(5) THE CLI, VERBATIM")
runner = CliRunner()
for args in (["memory", "list", "--uninjectable"], ["memory", "fix-scopes"],
             ["memory", "list", "--uninjectable"]):
    print("$ orchestrator", *args)
    print(runner.invoke(app, args).output.rstrip())
PY
  echo "DEMO_EXIT=$?"
} >"$TMP/checks.txt"

{
  echo "$STAMP"
  echo "# READ-ONLY audit of the operator's LIVE ORCHESTRATOR_HOME with this branch's rule."
  echo "# Nothing here writes: no CLI call (a CLI command runs _ensure_ready -> db.upgrade() on"
  echo "# the live DB with branch code, which this phase must not do), no fix_scopes, no"
  echo "# render_file. The DB is opened with sqlite3 mode=ro and the rows are judged by the"
  echo "# pure injectability() helper on duck-typed values."
  LIVE_HOME=/home/mrbigroom/.local/share/coding-orchestrator
  if [ -f "$LIVE_HOME/orchestrator.db" ]; then
    ORCHESTRATOR_HOME="$LIVE_HOME" PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" - <<'LIVEPY'
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from orchestrator.services import memory_service as m

HOME = Path(__import__("os").environ["ORCHESTRATOR_HOME"])
conn = sqlite3.connect(f"file:{HOME / 'orchestrator.db'}?mode=ro", uri=True)
rows = conn.execute(
    "select id, category, scope, project_slug, role, approved, rejected, source "
    "from memory_entries order by id"
).fetchall()
print("live rows:", len(rows), " by status:",
      conn.execute("select"
                   " sum(approved), sum(rejected),"
                   " sum(approved=0 and rejected=0) from memory_entries").fetchone(),
      "(approved, rejected, pending)")
print("by source:", conn.execute(
    "select source, count(*) from memory_entries group by source order by 2 desc"
).fetchall())
bad = []
for r in rows:
    e = SimpleNamespace(category=r[1], scope=r[2], project_slug=r[3], role=r[4],
                        approved=r[5], rejected=r[6])
    ok, reason = m.injectability(e)
    if not ok:
        bad.append((r[0], r[1], r[2], r[7],
                    "rejected" if r[6] else ("approved" if r[5] else "pending"), reason))
print("uninjectable under this branch's rule:", len(bad))
for b in bad:
    print("  #%s %s scope=%r source=%s status=%s :: %s" % b)
print("counter-examples that must NOT be flagged:")
for r in rows:
    if r[1] == "role":
        e = SimpleNamespace(category=r[1], scope=r[2], project_slug=r[3], role=r[4],
                            approved=r[5], rejected=r[6])
        print("  #%s role scope=%r role=%r -> injectable=%s reachable_by=%s" % (
            r[0], r[2], r[4], m.is_injectable(e), ",".join(m.reachable_roles(e)) or "-"))
tree = sorted(str(q.relative_to(HOME / "memory")) for q in (HOME / "memory").rglob("*.md"))
print("rendered files under memory/:", len(tree))
print("  canonical:", [f for f in tree if m.canonical_file(HOME / "memory" / f)])
print("  NOT canonical (would be collected by fix_scopes' GC if unowned):",
      [f for f in tree if not m.canonical_file(HOME / "memory" / f)])
owned = {
    m._file_for(SimpleNamespace(category=r[1], scope=r[2], project_slug=r[3], role=r[4]))
    for r in rows if r[5]
}
print("  of those, unowned by any approved row:",
      [str((HOME / "memory" / f).relative_to(HOME)) for f in tree
       if not m.canonical_file(HOME / "memory" / f)
       and str(HOME / "memory" / f) not in {str(p) for p in owned}])
print("  content of the measured ghost file:",
      repr((HOME / "memory/projects/ai-livestreaming-avatar/ai-livestreaming-avatar.md")
           .read_text()))
LIVEPY
    echo "LIVE_EXIT=$?"
  else
    echo "live home not present on this machine: skipped"
  fi
} >"$TMP/live_home_readonly.txt"

for f in full_suite.txt memory_scope_tests.txt checks.txt live_home_readonly.txt; do
  mv "$TMP/$f" "verify-evidence/T-4/$f"
done

grep -E "^(#|[0-9]+ (passed|failed|skipped)|All checks|SUITE_EXIT|TARGETED_EXIT|LINT_EXIT|DEMO_EXIT)" \
  verify-evidence/T-4/full_suite.txt \
  verify-evidence/T-4/memory_scope_tests.txt \
  verify-evidence/T-4/checks.txt \
  verify-evidence/T-4/live_home_readonly.txt
