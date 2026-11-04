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
  echo "# REVIEW ROUND 3 — the R-26 findings, each demonstrated on THIS worktree's code"
  echo "# in a throwaway ORCHESTRATOR_HOME. No network, no API keys, no live home touched."
  echo "# command: ORCHESTRATOR_HOME=\$TMP/home3 PYTHONPATH=\$WT/src \$MAIN/.venv/bin/python <demo>"
  env -u VIRTUAL_ENV -u ORCHESTRATOR_ENABLE_WORKER -u ORCHESTRATOR_ENABLE_SCHEDULER \
    ORCHESTRATOR_HOME="$TMP/home3" PYTHONPATH="$WT/src" COLUMNS=400 \
    "$MAIN/.venv/bin/python" - <<'PY3'
import os
from pathlib import Path

from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.core import db
from orchestrator.core.models import MemoryCategory, MemoryEntry
from orchestrator.core.schemas import MemoryCreate, MemoryUpdate
from orchestrator.services import bootstrap
from orchestrator.services import memory_service as m

HOME = Path(os.environ["ORCHESTRATOR_HOME"])
runner = CliRunner()
WIDE = {"COLUMNS": "400"}


def flat(s: str) -> str:
    return " ".join(s.split())


def rel(p) -> str:
    return str(Path(str(p)).relative_to(HOME / "memory"))


def cited(fn, needles: tuple, label: str) -> None:
    """Print the exact lines that do the checking, at the HEAD this log was captured at."""
    import inspect

    src, start = inspect.getsourcelines(fn)
    print(f"    {label}  ({fn.__module__}.{fn.__name__}, lines {start}-"
          f"{start + len(src) - 1}):")
    for i, line in enumerate(src, start):
        if any(n in line for n in needles):
            print(f"      :{i}: {line.rstrip()}")


bootstrap.init()
print("R3-FINDING 1 - category=role is guarded on the ROLE COLUMN, not only on `scope`")
print("  (DoD 2: 'category=role stops being only half-guarded by memory_service._validate')")
cited(m._validate, ("check_role_name(entry.role)", "if entry.role:", "entry.role = entry.scope"),
      "_validate refuses the role column before the scope")
cited(m.create, ("getattr(data.role",), "create() no longer dies on a raw role string")
print("  (1) service create(): role='logbook-writer' scope='worker' (a VALID scope)")
with db.session_scope() as s:
    body = MemoryCreate.model_construct(
        category=MemoryCategory.role, scope="worker", content="Diary.",
        role="logbook-writer", approved=False, tags=[], source="handoff:R-27",
        project_slug=None)
    before = len(m.list_entries(s))
    try:
        e = m.create(s, body)
        print("    NOT REJECTED ->", e.id, e.role, e.scope)
    except m.MemoryError_ as err:
        print("    refused:", err)
    print("    rows added:", len(m.list_entries(s)) - before,
          "| roles/logbook-writer.md created:",
          (HOME / "memory" / "roles" / "logbook-writer.md").exists())

print("  (2) PATCH / memory_service.update(): the row is ALREADY stored in that shape")
with db.session_scope() as s:
    dead = MemoryEntry(category="role", scope="worker", role="logbook-writer",
                       content="Diary.", source="handoff:R-27", approved=True)
    s.add(dead)
    s.commit()
    s.refresh(dead)
    m.render_file(s, m._file_for(dead))
    rid, home = dead.id, m._file_for(dead)
    before = home.read_text()
    print("    stored row:", rid, "role='logbook-writer' scope='worker' ->",
          rel(home), "injectable:", m.is_injectable(s.get(MemoryEntry, rid)))
    try:
        m.update(s, rid, MemoryUpdate(scope="worker"))  # the CALLER's scope is in vocabulary
        print("    NOT REJECTED")
    except m.MemoryError_ as err:
        print("    refused:", err)
    fresh = s.get(MemoryEntry, rid)
    print("    row after:", repr(fresh.role), repr(fresh.scope),
          "| file byte-identical:", home.read_text() == before)
    print("    content-only edit still possible:",
          repr(m.update(s, rid, MemoryUpdate(content="Reworded.")).content))

print("  (3) CLI `memory add --role`: the error names the role FILES and their aliases")
r = runner.invoke(app, ["memory", "add", "Diary.", "--category", "role", "--scope", "worker",
                        "--role", "logbook-writer"], env=WIDE)
print("    $ orchestrator memory add Diary. --category role --scope worker "
      "--role logbook-writer")
print("    exit", r.exit_code, "::", flat(r.output)[:250])
r = runner.invoke(app, ["memory", "add", "Gate the merge.", "--category", "role",
                        "--scope", "reviewer", "--role", "reviewer"], env=WIDE)
print("    $ orchestrator memory add Gate. --category role --scope reviewer --role reviewer")
print("    exit", r.exit_code, "::", flat(r.output)[:120])
with db.session_scope() as s:
    row = [e for e in m.list_entries(s, category="role") if e.content == "Gate the merge."]
    print("    stored:", [(e.role, e.scope, rel(m._file_for(e)), m.is_injectable(e))
                          for e in row],
          "| reachable_by:", m.reachable_roles(row[0]) if row else None)

print("  (4) HTTP POST /api/memory: the enum is a layer IN FRONT (422, frozen error shape)")
from fastapi.testclient import TestClient  # in-process ASGI, no socket, no network

from orchestrator.api.main import create_app

with TestClient(create_app(), base_url="http://127.0.0.1:8321") as client:
    resp = client.post("/api/memory", json={"category": "role", "scope": "worker",
                                            "role": "logbook-writer", "content": "Diary."})
    print("    POST status", resp.status_code, "keys", sorted(resp.json()),
          "| error:", resp.json()["error"])
    print("    detail names the offending value:",
          any("logbook-writer" in str(x) for x in resp.json()["detail"]))
    print("    (the PATCH boundary against a row ALREADY stored in that shape is shown in")
    print("     step (2) and asserted over HTTP by "
          "test_memory_scope_boundary.py::"
          "test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope)")

print("  (5) `memory import`: a dead role name is never stored, even from a payload")
with db.session_scope() as s:
    n = m.import_all(s, {"version": 1, "entries": [
        {"category": "role", "scope": "worker", "role": "logbook-writer",
         "content": "Diary.", "approved": True}]})
    print("    imported:", n, "| role rows with that content:",
          len([e for e in m.list_entries(s, category="role")
               if e.content == "Diary."]))

print("  (6) the counter-examples that must STAY injectable (#39/#47 shape)")
with db.session_scope() as s:
    good = MemoryEntry(category="role", scope="worker", role="worker",
                       content="Small commits.", source="handoff:R-27", approved=True)
    s.add(good)
    s.commit()
    s.refresh(good)
    m.render_file(s, m._file_for(good))
    print("    role='worker' scope='worker' ->", rel(m._file_for(good)),
          "injectable:", m.is_injectable(good), "reachable_by:", m.reachable_roles(good))
    for name in ("reviewer", "agent"):
        print("      check_role_name(%r) ->" % name, m.check_role_name(name),
              "files:", m.role_memory_file(name))

print("\nR3-FINDING 2 - `fix-scopes --gc-only` reports only the deletions it actually did")
print("  (DoD 3: the repair output said 'removed dead scope-file' for files that were still")
print("   on disk whenever gc_orphan_files() returned [], because of `removed or listed')")
cited(__import__("orchestrator.cli", fromlist=["x"]).memory_fix_scopes,
      ("repair = m.ScopeRepair(removed_files=m.gc_orphan_files(s)",
       "removed_files=m.orphan_files(s), dry_run=True)", "ONLY what was actually unlinked"),
      "the gc-only branch")
root = m.memory_root()
ghost = root / "projects" / "demo" / "demo.md"
ghost.parent.mkdir(parents=True, exist_ok=True)
ghost.write_text("# demo\n")  # the measured orphan: title line only
with db.session_scope() as s:
    print("  orphan_files():", [rel(p) for p in m.orphan_files(s)])
real_gc = m.gc_orphan_files
m.gc_orphan_files = lambda *a, **k: []  # a GC that unlinks nothing
out = runner.invoke(app, ["memory", "fix-scopes", "--gc-only"], env=WIDE)
m.gc_orphan_files = real_gc
d = flat(out.output)
print("  (a) GC returns [] while the listing is non-empty:")
print("      exit", out.exit_code, "| 'removed dead scope-file' appears?",
      "removed dead scope-file" in d)
print("      summary tail:", d[-70:])
print("      file still on disk:", ghost.exists(), "<- report and disk agree")
print("  (b) --gc-only --dry-run still shows what a real run would collect:")
dry = flat(runner.invoke(app, ["memory", "fix-scopes", "--gc-only", "--dry-run"],
                         env=WIDE).output)
print("      'would remove dead scope-file' named:",
      f"would remove dead scope-file {ghost}" in dry,
      "| '1 dead scope-file(s) to remove':", "1 dead scope-file(s) to remove" in dry,
      "| 'removed dead scope-file':", "removed dead scope-file" in dry,
      "| still present:", ghost.exists())
print("  (c) the real run, unpatched, deletes and then says so:")
run = flat(runner.invoke(app, ["memory", "fix-scopes", "--gc-only"], env=WIDE).output)
print("      'removed dead scope-file' named:", f"removed dead scope-file {ghost}" in run,
      "| file gone:", not ghost.exists())
assert "removed dead scope-file" not in d, "the false deletion claim is back"
assert f"removed dead scope-file {ghost}" in run and not ghost.exists()
print("  ASSERTIONS HELD: the report never claimed a deletion the disk contradicted.")
PY3
  echo "ROUND3_EXIT=$?"
} >"$TMP/review_round3.txt"

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
  name="diff_$(echo "$f" | tr '/' '_')"
  git diff "$BASE" HEAD -- "$f" >"$TMP/.raw.$name"
  total=$(wc -l <"$TMP/.raw.$name")
  parts=$(( (total + 159) / 160 ))
  [ "$parts" -lt 1 ] && parts=1
  split -l 160 -d "$TMP/.raw.$name" "$TMP/.part.$name."
  i=0
  for p in "$TMP"/.part.$name.*; do
    i=$((i + 1))
    {
      echo "$STAMP"
      echo "# git diff $BASE HEAD -- $f   PART $i/$parts"
      echo "# The COMPLETE per-file diff, cut at 160-line boundaries so that a reader with a"
      echo "# 2000-line/50KB view limit never sees a function chopped off mid-hunk: every diff"
      echo "# line appears in exactly one part, in order. Raw diff = $total lines."
      cat "$p"
      echo "DIFF_PART_COMPLETE=$f part $i/$parts"
    } >"$TMP/${name}.part${i}.txt"
  done
  rm -f "$TMP"/.part.$name.* "$TMP/.raw.$name"
done

{
  echo "$STAMP"
  echo "# FINAL SOURCE of the exact code the R-25/R-26 review could not inspect in a diff,"
  echo "# printed with line numbers AT THIS HEAD (a slice of the file as it now stands, not a"
  echo "# patch), split into <=180-line parts. Each file names the module and the line range."
  echo "# Areas: _validate/check_role_name/create/update (boundary rejection), fix_scopes +"
  echo "# gc_orphan_files/orphan_files (repair + GC), run_service._store_memory_suggestions"
  echo "# (the build-log substitution writes), cli.memory_add/cli.memory_fix_scopes (the CLI"
  echo "# boundaries + the repair report), and the round-3 test assertions."
  STAMP="$STAMP" OUTDIR="$TMP" PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" - <<'PY4'
import inspect
import os
import re
from pathlib import Path

OUT = Path(os.environ["OUTDIR"])
STAMP = os.environ["STAMP"]
PART_MAX = 180

from orchestrator import cli
from orchestrator.services import memory_service as m
from orchestrator.services import run_service

WT = Path.cwd()


def emit(base, header, lines):
    """Write one final-source slice, chunked so no view limit ever cuts a function off."""
    n = max(1, -(-len(lines) // PART_MAX))
    for i in range(n):
        chunk = lines[i * PART_MAX:(i + 1) * PART_MAX]
        name = f"{base}.part{i + 1}of{n}.txt" if n > 1 else f"{base}.txt"
        path = OUT / name
        path.write_text("\n".join([STAMP] + header + [f"# part {i + 1}/{n}"] + chunk) + "\n")
        print(f"  wrote {name} ({len(chunk)} lines)")


def dump_fn(base, fn, note):
    src, start = inspect.getsourcelines(fn)
    lines = [f"{i:>5} | {ln.rstrip()}" for i, ln in enumerate(src, start)]
    emit(base, [
        f"# FINAL SOURCE at this HEAD (the file as it now stands, NOT a patch): "
        f"{fn.__module__}.{fn.__qualname__}",
        f"# lines {start}-{start + len(src) - 1} of {inspect.getsourcefile(fn)}",
        f"# why the reviewer asked for it: {note}",
    ], lines)


def dump_lines(base, path, first, last, note):
    text = Path(path).read_text().splitlines()
    lines = [f"{i:>5} | {text[i - 1]}" for i in range(first, min(last, len(text)) + 1)]
    emit(base, [
        f"# FINAL SOURCE at this HEAD: {path} lines {first}-{min(last, len(text))}",
        f"# why the reviewer asked for it: {note}",
    ], lines)


def test_span(path, name):
    """The full text of one test function, numbered, from `def name(` to the next top-level def."""
    text = Path(path).read_text().splitlines()
    start = next(i for i, ln in enumerate(text, 1) if ln.startswith(f"def {name}("))
    end = len(text)
    for i in range(start, end):
        if i + 1 > start and re.match(r"^(def |# ---)", text[i]):
            end = i
            break
    return text[start - 1:end], start, end


def dump_tests(base, path, names, note):
    lines = []
    for name in names:
        body, start, _ = test_span(path, name)
        lines += [f""] + [f"{i:>5} | {ln}" for i, ln in enumerate(body, start)]
    emit(base, [
        f"# FINAL SOURCE at this HEAD: {path} — the assertions themselves, numbered",
        f"# tests: {', '.join(names)}",
        f"# why the reviewer asked for it: {note}",
    ], lines)


print("# final-source slices")
SVC = "src/orchestrator/services/memory_service.py"
TESTS = "tests"
dump_fn("slice_memory_service__validate", m._validate,
        "DoD 2 'the API and CLI create/update boundaries reject an out-of-vocabulary scope "
        "for EVERY category' — and, this round, the ROLE COLUMN before the scope")
dump_fn("slice_memory_service__check_role_name", m.check_role_name,
        "DoD 2 round-3 finding: the single message that names the role FILES and their aliases")
dump_fn("slice_memory_service__role_memory_file", m.role_memory_file,
        "the alias resolution (reviewer -> architect, agent -> worker)")
dump_fn("slice_memory_service__normalize_scope", m.normalize_scope,
        "DoD 2 the shared membership check")
dump_fn("slice_memory_service__coerce_scope", m.coerce_scope,
        "DoD 1 deterministic fallback, never an inference")
dump_fn("slice_memory_service__create", m.create,
        "DoD 2 create boundary; raw role strings are refused, not crashed on")
dump_fn("slice_memory_service__update", m.update,
        "DoD 2 update boundary: the throwaway preview the review could not inspect")
dump_fn("slice_memory_service__fix_scopes", m.fix_scopes,
        "DoD 3 the repair: re-render BOTH files, print ids, leave rejected rows alone")
dump_fn("slice_memory_service__gc_orphan_files", m.gc_orphan_files,
        "DoD 3 the orphan collection (the GC the review could not read)")
dump_fn("slice_memory_service__orphan_files", m.orphan_files,
        "DoD 3 what counts as dead output")
dump_fn("slice_memory_service___renderer_output", m._renderer_output,
        "DoD 3 the hand-edited-markdown protection predicate")
dump_fn("slice_memory_service__injectability", m.injectability,
        "DoD 4 the per-category reachability judgement")
dump_fn("slice_memory_service__reachable_roles", m.reachable_roles,
        "DoD 4 which prompt roles read a row; role column first, then scope")
dump_fn("slice_memory_service__import_all", m.import_all,
        "DoD 3 the other repair path (repairs scopes, never stores a dead role)")
dump_fn("slice_run_service__store_memory_suggestions", run_service._store_memory_suggestions,
        "DoD 1 'the substitution is recorded where the operator can see it': the build-log "
        "writes (memory_scope_substituted / memory_suggestion_dropped)")
dump_fn("slice_cli__memory_add", cli.memory_add,
        "DoD 2 the CLI create boundary, including this round's --role pre-check")
dump_fn("slice_cli__memory_fix_scopes", cli.memory_fix_scopes,
        "DoD 3 the repair report, including this round's gc-only honesty fix")
dump_fn("slice_cli__memory_list", cli.memory_list, "DoD 4 the list marker command")
dump_fn("slice_cli__print_injectability", cli._print_injectability,
        "DoD 4 the reason line under the table")

dump_lines("slice_memory_service__vocabularies", SVC, 29, 78,
           "the three named constants + FALLBACK_SCOPE + ROLE_PROJECT_SCOPES + ROLE_MEMORY_KEY")
dump_lines("slice_cli__gc_only_branch", "src/orchestrator/cli.py",
           next(i for i, ln in enumerate(
               Path("src/orchestrator/cli.py").read_text().splitlines(), 1)
               if "if gc_only:" in ln),
           next(i for i, ln in enumerate(
               Path("src/orchestrator/cli.py").read_text().splitlines(), 1)
               if "repair = m.fix_scopes(" in ln),
           "DoD 3 round-2 finding: `removed_files=removed or listed` claimed deletions")

UT = f"{TESTS}/unit/test_memory_scopes.py"
IT = f"{TESTS}/integration/test_memory_scope_boundary.py"
dump_tests("slice_tests_unit__role_column_boundary", UT, [
    "test_check_role_name_names_the_role_vocabulary",
    "test_create_refuses_an_unreadable_role_column_even_when_the_scope_is_valid",
    "test_update_refuses_a_legacy_role_column_that_no_prompt_reads",
    "test_import_refuses_an_unreadable_role_column_without_storing_it",
    "test_role_and_scope_disagreements_land_on_the_file_the_row_is_read_from",
], "DoD 2, the R-26 finding 1: the role column at create/PATCH/CLI/import")
dump_tests("slice_tests_unit__coercion_and_log", UT, [
    "test_coercion_falls_back_and_never_infers_from_the_words_in_the_scope",
    "test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored",
    "test_coercion_is_written_to_the_build_log_the_operator_reads",
], "DoD 1 coercion + the substitution record")
dump_tests("slice_tests_unit__repair_and_gc", UT, [
    "test_fix_scopes_moves_legacy_rows_and_re_renders_both_files",
    "test_fix_scopes_leaves_rejected_rows_alone",
    "test_gc_only_deletes_files_render_file_could_have_written",
], "DoD 3 re-rendering both files, and the GC predicate")
dump_tests("slice_tests_unit__list_marker", UT, [
    "test_injectability_marks_only_the_unreachable",
    "test_role_rows_still_render_by_the_rule_main_used",
], "DoD 4 the marker, and the #39/#47 counter-examples")
dump_tests("slice_tests_integration__gc_only_honesty", IT, [
    "test_cli_fix_scopes_gc_only_reports_only_the_files_it_actually_deleted",
    "test_cli_add_refuses_a_role_name_no_prompt_can_read",
    "test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope",
], "the two R-26 findings, at the real CLI/HTTP boundaries")
PY4
} >"$TMP/slices_index.txt"

# ------------------------------------------------------------------ stage everything
# Replace this directory's captured outputs wholesale: the per-file diffs are named
# differently now (.partN), and a stale file from an earlier round left beside them would
# be read as if it were current.
rm -f verify-evidence/T-4/diff_*.txt verify-evidence/T-4/slice_*.txt
mv "$TMP"/*.txt "verify-evidence/T-4/"

echo
echo "== captured files =="
ls -1 verify-evidence/T-4
echo
echo "== exits =="
grep -H -E "^(SUITE_EXIT|TARGETED_EXIT|LINT_EXIT|DEMO_EXIT|ROUND2_EXIT|ROUND3_EXIT)=" \
  verify-evidence/T-4/*.txt
echo
echo "== counts =="
grep -H -E "^[0-9]+ (passed|failed)|All checks passed" verify-evidence/T-4/full_suite.txt \
  verify-evidence/T-4/memory_scope_tests.txt verify-evidence/T-4/checks.txt
