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
# The dirty-entry count excludes this directory's own outputs, which are being rewritten by
# this very run; anything else dirty would still show up.
DIRTY=$(git status --porcelain | grep -v '^.. verify-evidence/T-4/' | wc -l)
STAMP="# HEAD=$HEAD ($(git rev-parse --short HEAD)) branch=$(git branch --show-current) captured=$(date -Is) tree=$DIRTY dirty-entries (excluding verify-evidence/T-4)"
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

{
  echo "$STAMP"
  echo "# REVIEW ROUND 2 — the R-25 findings, each demonstrated on THIS worktree's code"
  echo "# in a throwaway ORCHESTRATOR_HOME. No network, no API keys."
  echo "# command: ORCHESTRATOR_HOME=\$TMP/home2 PYTHONPATH=\$WT/src \$MAIN/.venv/bin/python <demo>"
  env -u VIRTUAL_ENV -u ORCHESTRATOR_ENABLE_WORKER -u ORCHESTRATOR_ENABLE_SCHEDULER \
    ORCHESTRATOR_HOME="$TMP/home2" PYTHONPATH="$WT/src" COLUMNS=400 \
    "$MAIN/.venv/bin/python" - <<'PY2'
import json
import os
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.core import db
from orchestrator.core.models import MemoryCategory, MemoryEntry
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion, MemoryUpdate
from orchestrator.services import bootstrap
from orchestrator.services import memory_service as m

HOME = Path(os.environ["ORCHESTRATOR_HOME"])
runner = CliRunner()


def rel(p) -> str:
    return str(Path(str(p)).relative_to(HOME / "memory"))


def flat(out: str) -> str:
    return " ".join(out.split())


bootstrap.init()
print("FINDING 1 - coercion is the literal fallback, never an inference from the words")
for category, scope in (
    (MemoryCategory.project, "decisions-and-gating"),
    (MemoryCategory.project, "decisionss"),
    (MemoryCategory.project, "conventions-notes"),
    (MemoryCategory.global_, "patterns-of-the-team"),
):
    got, note = m.coerce_scope(category, scope)
    print(f"  {category}/{scope!r} -> {got!r}   note: {note}")
print("  in vocabulary stays put and is silent:",
      m.coerce_scope(MemoryCategory.project, "decisions"))
with db.session_scope() as s:
    out = m.suggest_from_handoff_report(
        s,
        [MemorySuggestion(category="project", scope="decisions-about-the-gate",
                          content="Gate the merge.")],
        "handoff:R-26", "demo",
    )
    e = out.created[0]
    print(f"  stored scope={e.scope!r} reachable_by={m.reachable_roles(e)} "
          f"(an inference would have said 'decisions' -> architect,supervisor)")

print("\nFINDING 2 - Rich markup in the repair output (brackets were eaten; now escaped)")
console = Console(record=True, width=200)
console.print("left #1 project scope '[stale]' alone [rejected rows are left alone]")
print("  what the OLD bracketed line printed:", repr(console.export_text().strip()))
with db.session_scope() as s:
    s.add(MemoryEntry(category="project", scope="[stale]", project_slug="demo",
                      content="Bracketed.", source="handoff:R-26", rejected=True))
    s.commit()
    raw = runner.invoke(app, ["memory", "fix-scopes", "--dry-run"],
                        env={"COLUMNS": "400"}).output
    got = flat(raw)
    line = [" ".join(ln.split()) for ln in raw.splitlines() if "stale" in ln]
    print("  the CLI now prints:", line)
    assert "'[stale]'" in got and "rejected rows are left alone" in got

print("\nFINDING 3 - --dry-run promises, it does not report; each ghost is named once")
with db.session_scope() as s:
    bad = MemoryEntry(category="global", scope="[dim]notes", content="Escaped.",
                      source="handoff:R-26", approved=True)
    s.add(bad)
    s.commit()
    s.refresh(bad)
    bad_id = bad.id
    m.render_file(s, m._file_for(bad))
    ghost = m.memory_root() / "global" / "dim-notes.md"
    already = m.memory_root() / "global" / "zz-dead.md"
    already.write_text("# zz-dead\n")
    dry = flat(runner.invoke(app, ["memory", "fix-scopes", "--dry-run"],
                             env={"COLUMNS": "400"}).output)
    print("  dry: 'would re-render' x%s, 'would collect dead scope-file' x%s, "
          "'would remove dead scope-file' x%s"
          % (dry.count("would re-render"), dry.count("would collect dead scope-file"),
             dry.count("would remove dead scope-file")))
    for lie in ("moved #", "re-rendered", "removed dead scope-file", "collected "):
        assert lie not in dry, lie
    print("  dry: says 're-rendered'?", "re-rendered" in dry, "| 'moved #'?", "moved #" in dry)
    assert ghost.exists() and already.exists()
    run = flat(runner.invoke(app, ["memory", "fix-scopes"], env={"COLUMNS": "400"}).output)
    print("  run: 'removed dead scope-file' x%s (one line each), 'collected' x%s"
          % (run.count("removed dead scope-file"), run.count("collected")))
    print("  ghost gone?", not ghost.exists(), "| already-dead gone?", not already.exists())
with db.session_scope() as s2:  # a FRESH session: `s` still caches the pre-move row
    print("  row after the move (re-read):", [(e.id, e.scope) for e in m.list_entries(s2)
                                              if e.id == bad_id])

print("\nFINDING 5 - the GC deletes ONLY text render_file could have written")
proj = m.memory_root() / "projects" / "demo"
proj.mkdir(parents=True, exist_ok=True)
hand = proj / "notes.md"
hand.write_text("# Hand-written notes\n\n- a line someone typed by hand\n")
prose = proj / "scratch.md"
prose.write_text("# scratch\n\nremember the mount path does not exist\n")
header = proj / "conventions.md"
header.write_text("# Project demo — conventions\n")
ghost = proj / "demo.md"
ghost.write_text("# demo\n")
marker = proj / "old-scope.md"
marker.write_text("# old-scope\n\n- dead line  <!-- mem:999 -->\n")
print("  predicate: in_layout_dir AND not canonical_file AND not owned AND _renderer_output")
with db.session_scope() as s:
    owned = {m._file_for(e) for e in m.list_entries(s, status="approved")}
    for p in (hand, prose, header, ghost, marker):
        print(f"    {rel(p):26} layout={m.in_layout_dir(p)!s:5} "
              f"canonical={m.canonical_file(p)!s:5} owned={(p in owned)!s:5} "
              f"renderer_output={m._renderer_output(p)!s:5}")
    print("  orphan_files:", sorted(rel(p) for p in m.orphan_files(s)))
    removed = sorted(rel(p) for p in m.gc_orphan_files(s))
    print("  gc deleted:", removed)
    print("  hand-written survived:", hand.exists(), prose.exists(), header.exists())
    print("  dead renderer output deleted:", not ghost.exists(), not marker.exists())

print("\nFINDING 6 - a refused PATCH changes nothing (no rollback needed)")
with db.session_scope() as s:
    row = m.create(s, MemoryCreate(category="project", scope="gotchas", project_slug="demo",
                                   content="Watch out.", approved=True))
    home = m._file_for(row)
    before = home.read_text()
    try:
        m.update(s, row.id, MemoryUpdate(scope="housekeeping", content="Reworded."))
        print("  NOT REJECTED")
    except m.MemoryError_ as err:
        print("  rejected:", err)
    fresh = s.get(MemoryEntry, row.id)
    print(f"  row after: scope={fresh.scope!r} content={fresh.content!r}")
    print("  file byte-identical:", home.read_text() == before,
          "| session dirty/new:", bool(s.dirty), bool(s.new))

print("\nFINDING 7 - role rendering is STILL main's rule: role, else scope, ROLE_MEMORY_KEY")
with db.session_scope() as s:
    dead = MemoryEntry(category="role", scope="worker", role="logbook-writer",
                       content="Diary.", source="handoff:R-26", approved=True)
    good = MemoryEntry(category="role", scope="worker", role="worker",
                       content="Small commits.", source="handoff:R-26", approved=True)
    for r in (dead, good):
        s.add(r)
        s.commit()
        s.refresh(r)
        m.render_file(s, m._file_for(r))
    for e in (dead, good):
        ok, reason = m.injectability(e)
        print(f"  #{e.id} role={e.role!r} scope={e.scope!r} -> file {rel(m._file_for(e))} "
              f"injectable={ok} reachable_by={m.reachable_roles(e) or '-'}")
        print(f"     reason: {reason or '-'}")
    print("  worker.md last line:",
          (m.memory_root() / "roles" / "worker.md").read_text().strip().splitlines()[-1])
    rep = m.fix_scopes(s)
    print("  fix-scopes moved:", rep.moved_ids, "| left alone:",
          [(x["id"], x["why"][:58]) for x in rep.skipped])

print("\nFINDING 8 - rejecting vs repairing, side by side at the CLI")
with db.session_scope() as s:
    s.add(MemoryEntry(category="project", scope="logbook", project_slug="demo",
                      content="Legacy fact.", source="handoff:R-26", approved=False))
    s.commit()
    payload = json.loads(m.dumps_export(s))
exp = HOME / "mem.json"
exp.write_text(json.dumps(payload))
print("  $ orchestrator memory add x --category project --scope logbook --project demo")
print("   ", flat(runner.invoke(app, ["memory", "add", "x", "--category", "project",
                                      "--scope", "logbook", "--project", "demo"]).output)[:140])
print("  $ orchestrator memory import mem.json   (REPAIRS the legacy export scope)")
print("   ", flat(runner.invoke(app, ["memory", "import", str(exp)]).output))
with db.session_scope() as s:
    print("  project rows now:", [(e.id, e.scope, e.content) for e in
                                   m.list_entries(s, category="project")])
print("  $ orchestrator memory import mem.json   (again: idempotent)")
print("   ", flat(runner.invoke(app, ["memory", "import", str(exp)]).output))
print("  IMPORT NEVER MANUFACTURES A SECOND COPY of a fact that is already stored, even")
print("  while that row still carries its dead scope (the dedupe checks both spellings).")
with db.session_scope() as s:
    before = len(m.list_entries(s))
    print("   ", "re-import the live export itself:", m.import_all(s, json.loads(m.dumps_export(s))),
          "new rows (rows stored:", before, "->", len(m.list_entries(s)), ")")
print("  A fact the home does NOT have yet is still repaired, not dropped:")
fresh = HOME / "mem3.json"
fresh.write_text(json.dumps({"version": 1, "entries": [
    {"category": "project", "scope": "logbook", "project_slug": "other",
     "content": "A fact only this export has.", "approved": False},
    {"category": "role", "scope": "logbook-writer", "content": "Unplaceable."},
]}))
print("   ", flat(runner.invoke(app, ["memory", "import", str(fresh)]).output))
with db.session_scope() as s:
    print("   ", "stored as:", [(e.scope, e.project_slug, e.content) for e in
                                  m.list_entries(s, category="project") if e.project_slug == "other"],
          "| role rows:", len(m.list_entries(s, category="role")))
PY2
  echo "ROUND2_EXIT=$?"
} >"$TMP/review_round2.txt"

{
  echo "$STAMP"
  echo "# Every file this branch changes, as a COMPLETE per-file diff vs the merge-base."
  echo "# Split per file because the supplied combined diff was truncated mid-function; each"
  echo "# diff_*.txt beside this file is self-contained and ends with its own DIFF_COMPLETE"
  echo "# trailer. Base: $(git merge-base main HEAD)"
  BASE=$(git merge-base main HEAD)
  echo "BASE=$BASE HEAD=$(git rev-parse HEAD)"
  git diff --stat "$BASE" HEAD -- . ':(exclude)verify-evidence'
  echo "CHANGED_FILES_SHA256_AND_LENGTH"
  for f in $(git diff --name-only "$BASE" HEAD -- . ':(exclude)verify-evidence'); do
    printf '%s  %s  lines=%s\n' "$(sha256sum "$f" | cut -c1-16)" "$f" "$(wc -l <"$f")"
  done
} >"$TMP/manifest.txt"

BASE=$(git merge-base main HEAD)
for f in $(git diff --name-only "$BASE" HEAD -- . ':(exclude)verify-evidence'); do
  {
    echo "$STAMP"
    echo "# git diff $BASE HEAD -- $f  (COMPLETE: $(git diff "$BASE" HEAD -- "$f" | wc -l) diff lines)"
    git diff "$BASE" HEAD -- "$f"
    echo "DIFF_COMPLETE=$f"
  } >"$TMP/diff_$(echo "$f" | tr '/' '_').txt"
done

for f in full_suite.txt memory_scope_tests.txt checks.txt live_home_readonly.txt \
         review_round2.txt manifest.txt; do
  mv "$TMP/$f" "verify-evidence/T-4/$f"
done
for f in "$TMP"/diff_*.txt; do mv "$f" "verify-evidence/T-4/"; done

grep -E "^(#|[0-9]+ (passed|failed|skipped)|All checks|SUITE_EXIT|TARGETED_EXIT|LINT_EXIT|DEMO_EXIT|ROUND2_EXIT)" \
  verify-evidence/T-4/full_suite.txt \
  verify-evidence/T-4/memory_scope_tests.txt \
  verify-evidence/T-4/checks.txt \
  verify-evidence/T-4/live_home_readonly.txt \
  verify-evidence/T-4/review_round2.txt
