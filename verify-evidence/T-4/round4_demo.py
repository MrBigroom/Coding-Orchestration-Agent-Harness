"""T-4 round 4: the four operator items, demonstrated offline on THIS worktree's code.

Run by `capture.sh` inside a throwaway `ORCHESTRATOR_HOME` (no network, no API keys, the live
home is never opened). Every claim it makes about the code is checked in the same process, so a
regression shows up as a NON-ZERO exit instead of as prose in a handoff. The mutations in ITEM 3
are runtime patches of THIS process only (`m.render_file`, `m.gc_orphan_files`): no committed
file is edited and nothing outside the throwaway home changes.

ITEM 1  `files_changed` means paths that exist ON THE BRANCH.
ITEM 2  Every boundary's rejection TEXT names its own vocabulary (a status code is not proof).
ITEM 3  One assertion per outcome: the repair's re-render and the repair's deletion are separate
        cases, and a mutation shows the old disjunctive assertion proved neither.
ITEM 4  One HEAD, one stamp: prints the tree these numbers were measured at.
"""

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from typer.testing import CliRunner

from orchestrator.api.main import create_app
from orchestrator.cli import app
from orchestrator.core import db
from orchestrator.core.memory_vocab import ROLE_SCOPES, check_role_name
from orchestrator.core.models import MemoryEntry, Role
from orchestrator.core.schemas import MemoryCreate
from orchestrator.services import bootstrap
from orchestrator.services import memory_service as m

HOME = Path(os.environ["ORCHESTRATOR_HOME"])
WT = Path.cwd()
MERGE_BASE = "db00112"
runner = CliRunner()
WIDE = {"COLUMNS": "400", "TERMINAL_WIDTH": "400"}
fails: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))
    if not cond:
        fails.append(label)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(WT), capture_output=True, text=True,
                          check=True).stdout


def flat(text: str) -> str:
    return " ".join(text.split())


def legacy(session, *, scope: str, slug: str, content: str) -> MemoryEntry:
    """A row stored the way the unvalidated suggestion path used to store it, rendered."""
    row = MemoryEntry(category="project", scope=scope, project_slug=slug, content=content,
                      source="handoff:R-3", approved=True)
    session.add(row)
    session.commit()
    session.refresh(row)
    m.render_file(session, m._file_for(row))
    return row


bootstrap.init()

# ------------------------------------------------------------------ ITEM 1
print("ITEM 1 - files_changed = paths ON THE BRANCH (git diff --name-only <base>..HEAD)")
branch_files = [f for f in git("diff", "--name-only", f"{MERGE_BASE}..HEAD").split("\n") if f]
print(f"  base {MERGE_BASE} -> HEAD {git('rev-parse', 'HEAD').strip()}: "
      f"{len(branch_files)} changed paths")
in_tree = [f for f in branch_files if git("ls-tree", "HEAD", "--", f).strip()]
ok("every changed path exists in HEAD's tree", len(in_tree) == len(branch_files),
   f"{len(in_tree)}/{len(branch_files)}")
bad = [f for f in branch_files if f.startswith(".orchestrator/") or f.endswith(".log")]
ok("no gitignored path (.orchestrator/**, *.log) is in the branch diff", bad == [], str(bad))
print("  `.orchestrator/handoff.json` is ignored by:",
      git("check-ignore", "-v", ".orchestrator/handoff.json").split("\n")[0])
print("  -> the handoff's files_changed is GENERATED from this list, never typed by hand:")
for f in branch_files:
    print("    ", f)

# ------------------------------------------------------------------ ITEM 2
print("\nITEM 2 - the rejection TEXT at every boundary names the allowed values")
VOCAB = {"global": m.GLOBAL_SCOPES, "project": m.PROJECT_SCOPES, "role": m.ROLE_SCOPES}
BAD_SCOPE = {"global": "global-environment-verifying-orchestrator-change",
             "project": "ai-livestreaming-avatar-logbook",
             "role": "ai-livestreaming-avatar-logbook"}

with TestClient(create_app(), base_url="http://127.0.0.1:8321") as client:
    print("  (a) POST /api/memory with an out-of-vocabulary SCOPE, one case per category")
    for category, vocab in VOCAB.items():
        scope = BAD_SCOPE[category]
        body = {"category": category, "scope": scope, "content": "x"}
        if category == "project":
            body["project_slug"] = "demo"
        r = client.post("/api/memory", json=body)
        err = flat(str(r.json().get("error")))
        joined = "|".join(vocab)
        print(f"    {category}: {r.status_code} :: {err}")
        ok(f"POST {category} names {joined}", joined in err)
        ok(f"POST {category} names the refused value", scope in err)
        ok(f"POST {category} keeps the frozen error shape",
           set(r.json()) == {"error", "detail"}, str(sorted(r.json())))

    print("  (b) POST /api/memory with an unreadable ROLE COLUMN (R-27 finding 5: the message")
    print("      was never asserted). The 422 must carry the role FILES, not just a code.")
    r = client.post("/api/memory", json={"category": "role", "scope": "worker",
                                         "role": "logbook-writer", "content": "Diary."})
    body = r.json()
    detail = body.get("detail") or []
    msg = flat(str(detail[0].get("msg"))) if detail else "<no detail>"
    text = flat(" ".join(str(x) for x in detail) + " " + str(body.get("error")))
    print(f"    status {r.status_code}, body keys {sorted(body)}")
    print(f"    detail[0][msg]: {msg}")
    ok("the 422 names the vocabulary as architect|worker|supervisor",
       "|".join(ROLE_SCOPES) in text)
    ok("the 422 says 'allowed role memory files' (FILES, not enum values)",
       "allowed role memory files" in text)
    ok("the 422 spells out both aliases",
       "reviewer -> architect" in text and "agent -> worker" in text)
    ok("the 422 names the refused value", "logbook-writer" in text)
    print("    for contrast, what Pydantic's enum ALONE answers - the message DoD 2 refuses:")
    try:
        TypeAdapter(Role).validate_python("logbook-writer")
        bare = "...the enum accepted it, which would itself be a regression"
    except ValidationError as e:
        bare = flat(str(e.errors()[0]["msg"]))
    print("      enum-only msg:", bare)
    ok("the enum-only message does NOT contain the joined vocabulary, so the assertion above "
       "is load-bearing and not satisfiable by the enum text", "|".join(ROLE_SCOPES) not in bare)
    print("    the ACCEPTED set is unchanged: all five Role members still POST, aliases incl.")
    for name in ("architect", "worker", "supervisor", "reviewer", "agent"):
        rr = client.post("/api/memory", json={"category": "role", "scope": name,
                                              "role": name, "content": f"{name} note."})
        got = rr.json().get("scope")
        print(f"      role={name:11} -> {rr.status_code} stored scope={got}")
        ok(f"POST accepts role={name}", rr.status_code == 201)
    with db.session_scope() as s:
        ok("nothing was stored under the refused role name",
           [e for e in m.list_entries(s, category="role") if e.role == "logbook-writer"] == [])
    ok("no dead role file was rendered",
       not (HOME / "memory" / "roles" / "logbook-writer.md").exists())

print("  (c) the request model itself, with no HTTP in the picture")
try:
    MemoryCreate(category="role", scope="worker", content="x", role="logbook-writer")
    ok("MemoryCreate refuses an unknown role", False, "it did not raise")
except ValidationError as e:
    text = flat(str(e))
    ok("MemoryCreate's message names the vocabulary and the FILES",
       "|".join(ROLE_SCOPES) in text and "allowed role memory files" in text, text[:160])
ok("check_role_name passes every role name that resolves to a memory file",
   [check_role_name(n) for n in ("architect", "worker", "supervisor", "reviewer", "agent")]
   == ["architect", "worker", "supervisor", "reviewer", "agent"])

print("  (d) CLI `memory add`, one case per category, asserted on the words it prints")
for category, vocab in VOCAB.items():
    scope = BAD_SCOPE[category]
    argv = ["memory", "add", "x", "--category", category, "--scope", scope]
    if category == "project":
        argv += ["--project", "demo"]
    r = runner.invoke(app, argv, env=WIDE)
    out = flat(r.output)
    joined = "|".join(vocab)
    print(f"    {category}: exit {r.exit_code} :: {out[:150]}")
    ok(f"CLI {category} names {joined}", joined in out)
r = runner.invoke(app, ["memory", "add", "Diary.", "--category", "role", "--scope", "worker",
                        "--role", "logbook-writer"], env=WIDE)
print("    --role: exit", r.exit_code, "::", flat(r.output)[:150])
ok("CLI --role names the role files", "|".join(ROLE_SCOPES) in flat(r.output))

# ------------------------------------------------------------------ ITEM 3
print("\nITEM 3 - the repair: one assertion per outcome, and what the old disjunction proved")
root = m.memory_root()

print("  CASE A - gc=False. The dead file SURVIVES, so its content can be judged.")
with db.session_scope() as s:
    a = legacy(s, scope="case-a", slug="case-a",
               content="The repo is reachable directly on this host.")
    a_other = m.create(s, MemoryCreate(category="project", scope="gotchas",
                                       project_slug="case-a", approved=True,
                                       content="Keep this line."))
    old_a = root / "projects" / "case-a" / "case-a.md"
    new_a = root / "projects" / "case-a" / "gotchas.md"
    print(f"    before: {old_a.relative_to(root)} holds {old_a.read_text()!r}")
    rep_a = m.fix_scopes(s, gc=False)
    print(f"    moved : {[(x['id'], x['from'], x['to']) for x in rep_a.moved]}")
    ok("A1 the old file is still on disk (gc=False deleted nothing)", old_a.exists())
    ok("A2 the old file was RE-RENDERED to exactly the renderer's empty output",
       old_a.read_text() == "# case-a\n", repr(old_a.read_text()))
    ok("A3 nothing at all was reported as deleted", rep_a.removed_files == [],
       str(rep_a.removed_files))
    ok("A4 the dead file is still REPORTED, so --no-gc cannot hide it",
       [str(p) for p in rep_a.ghost_files] == [str(old_a)])
    ok("A5 the new file holds both lines in id order",
       new_a.read_text() == ("# gotchas\n\n"
                             f"- {a.content}  <!-- mem:{a.id} -->\n"
                             f"- Keep this line.  <!-- mem:{a_other.id} -->\n"),
       repr(new_a.read_text()))

print("  MUTATION 1 - keep the deletion, drop the old file's re-render.")
real_render = m.render_file
skip_to: list[Path] = []


def render_skipping(session, path):
    if path in skip_to:
        return  # the bug under test: the file the row moved away from is left as it was
    real_render(session, path)


with db.session_scope() as s:
    m1 = legacy(s, scope="mutant-one", slug="mutant-one",
                content="The /sessions/<id>/mnt mount in briefs does not exist.")
    old_m1 = root / "projects" / "mutant-one" / "mutant-one.md"
    skip_to.append(old_m1)
    m.render_file = render_skipping
    rep_m1 = m.fix_scopes(s)  # gc ON, exactly like the CLI's default
    disjunction = (not old_m1.exists()) or (f"mem:{m1.id}" not in old_m1.read_text())
    exact = old_m1.exists() and old_m1.read_text() == "# mutant-one\n"
    print(f"    the R-27 disjunctive assertion (`not exists or marker gone`) : "
          f"{'PASSES' if disjunction else 'fails'}  <- it cannot tell deletion from re-render")
    print(f"    CASE A's exact-text assertion, same mutation with gc=False   : "
          f"{'PASSES' if exact else 'FAILS as it must'}")
    ok("the mutation slips past the OLD disjunction and is caught by CASE A's shape",
       disjunction and not exact)
    # and the same with gc=False, where the stale line is left visible on disk
    skip_to.clear()
    m.render_file = real_render
    s2row = legacy(s, scope="mutant-kept", slug="mutant-kept", content="Visible stale line.")
    old_m2 = root / "projects" / "mutant-kept" / "mutant-kept.md"
    skip_to.append(old_m2)
    m.render_file = render_skipping
    m.fix_scopes(s, gc=False)
    stale_visible = old_m2.exists() and f"mem:{s2row.id}" in old_m2.read_text()
    print(f"    with gc=False the same mutation leaves the stale line ON DISK : "
          f"{'yes, so CASE A fails on it' if stale_visible else 'no'}")
    ok("a no-re-render mutant leaves exactly the stale line CASE A forbids", stale_visible)
    skip_to.clear()
    m.render_file = real_render
    # that demo ran with gc=False, so its dead file is still here; the next case must be
    # about its OWN file and nothing else, so clear it (the gotchas.md beside it stays).
    old_m2.unlink()

print("  CASE B - gc on (the CLI default). The dead file is GONE. The other single outcome.")
with db.session_scope() as s:
    b = legacy(s, scope="case-b", slug="case-b", content="A fact worth keeping.")
    old_b = root / "projects" / "case-b" / "case-b.md"
    new_b = root / "projects" / "case-b" / "gotchas.md"
    print(f"    orphans before: {[str(p.relative_to(root)) for p in m.orphan_files(s)]}")
    rep_b = m.fix_scopes(s)
    ok("B1 the old file no longer exists", not old_b.exists())
    ok("B2 removed_files names exactly that file and nothing else",
       rep_b.removed_files == [old_b], str(rep_b.removed_files))
    ok("B3 the moved row's line lives in the file an injector reads",
       f"- {b.content}  <!-- mem:{b.id} -->" in new_b.read_text(), repr(new_b.read_text()))
    ok("B4 a run reports no ghost files when the GC did its job", rep_b.ghost_files == [])

print("  MUTATION 2 - disable the collector: CASE B's assertion fails, CASE A's still holds.")
real_gc = m.gc_orphan_files
m.gc_orphan_files = lambda session, dry_run=False: []
with db.session_scope() as s:
    c = legacy(s, scope="mutant-two", slug="mutant-two", content="Another dead scope.")
    old_c = root / "projects" / "mutant-two" / "mutant-two.md"
    rep_c = m.fix_scopes(s)
    print(f"    removed_files under the mutation: {rep_c.removed_files} "
          f"(the assertion `== [old file]` therefore fails)")
    ok("the collector mutation is caught by CASE B's assertion",
       rep_c.removed_files != [old_c])
    ok("and the file is really still on disk, so any claim of deletion would be a lie",
       old_c.exists())
    ok("CASE A's own outcome (re-render) is unaffected by the collector mutation",
       old_c.read_text() == "# mutant-two\n", repr(old_c.read_text()))
m.gc_orphan_files = real_gc

# ------------------------------------------------------------------ ITEM 4
print("\nITEM 4 - one HEAD, one stamp")
print("  HEAD                :", git("rev-parse", "HEAD").strip())
print("  branch              :", git("branch", "--show-current").strip())
dirty = [x for x in git("status", "--porcelain").split("\n") if x]
print("  dirty entries at capture:", len(dirty), dirty)
print("  full_suite.txt, checks.txt, memory_scope_tests.txt, review_round4.txt and this demo")
print("  all begin with the same `# HEAD=` line, so every count is attributable to one tree.")

print("\nSUMMARY:", "ALL CHECKS HELD" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
