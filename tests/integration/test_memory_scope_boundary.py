"""T-4 DoD 2/3/4 at the real boundaries: the API rejects, the CLI rejects, repairs and marks.

Offline: in-process TestClient and CliRunner against an isolated ORCHESTRATOR_HOME.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.core.db import session_scope
from orchestrator.core.models import MemoryEntry
from orchestrator.services import memory_service as m

runner = CliRunner()

# Rich folds long lines at 80 columns, which would break an assertion about a whole output
# line (the memory root path in a tmp dir is longer than that). Widen the console and compare
# on whitespace-normalized text so the assertions are about the WORDS, not the wrapping.
WIDE = {"COLUMNS": "400", "TERMINAL_WIDTH": "400"}


def flat(out: str) -> str:
    return " ".join(out.split())


@pytest.fixture
def client(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8321") as c:
        yield c


def legacy(session, **kw) -> MemoryEntry:
    kw.setdefault("content", "A fact.")
    entry = MemoryEntry(
        category=kw["category"],
        scope=kw["scope"],
        project_slug=kw.get("project_slug"),
        role=kw.get("role"),
        content=kw["content"],
        source=kw.get("source", "handoff:R-3"),
        approved=kw.get("approved", False),
        rejected=kw.get("rejected", False),
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    if entry.approved:
        m.render_file(session, m._file_for(entry))
    return entry


# ------------------------------------------------------------------ API boundary


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            {"category": "global", "scope": "global-environment-verifying", "content": "x"},
            "environment|preferences|patterns",
        ),
        (
            {"category": "project", "scope": "ai-livestreaming-avatar", "project_slug": "demo",
             "content": "x"},
            "conventions|decisions|gotchas",
        ),
        (
            {"category": "role", "scope": "ai-livestreaming-avatar-logbook", "content": "x"},
            "architect|worker|supervisor",
        ),
    ],
)
def test_api_create_rejects_an_uninjectable_scope_for_every_category(client, body, expected):
    r = client.post("/api/memory", json=body)
    assert r.status_code == 400
    err = r.json()
    assert set(err) == {"error", "detail"}  # the frozen error shape
    assert expected in err["error"] and body["scope"] in err["error"]
    assert client.get("/api/memory", params={"scope": body["scope"]}).json() == []


def test_api_create_still_accepts_the_vocabulary_including_role_aliases(client):
    for body in (
        {"category": "global", "scope": "patterns", "content": "a"},
        {"category": "project", "scope": "gotchas", "project_slug": "demo", "content": "a"},
        {"category": "role", "scope": "reviewer", "content": "a"},
    ):
        r = client.post("/api/memory", json=body)
        assert r.status_code == 201, r.json()
        assert r.json()["scope"] in m.SCOPE_VOCABULARY[body["category"]]


def test_api_patch_rejects_an_uninjectable_scope(client):
    mid = client.post(
        "/api/memory",
        json={"category": "project", "scope": "gotchas", "project_slug": "demo",
              "content": "Watch out."},
    ).json()["id"]
    r = client.patch(f"/api/memory/{mid}", json={"scope": "housekeeping"})
    assert r.status_code == 400 and "conventions|decisions|gotchas" in r.json()["error"]
    got = [e for e in client.get("/api/memory").json() if e["id"] == mid][0]
    assert got["scope"] == "gotchas"
    assert client.patch(f"/api/memory/{mid}", json={"scope": "decisions"}).json()["scope"] == (
        "decisions"
    )


def test_api_patch_still_edits_the_content_of_a_legacy_row(client):
    """A stored bad scope must not freeze the row out of the API.

    `fix-scopes` is the repair path; refusing a content edit would leave the operator no way
    to fix the wording of an entry they already have.
    """
    with session_scope() as s:
        row_id = legacy(s, category="project", scope="logbook", project_slug="demo",
                        content="Old wording.").id
    r = client.patch(f"/api/memory/{row_id}", json={"content": "New wording."})
    assert r.status_code == 200 and r.json()["content"] == "New wording."
    assert r.json()["scope"] == "logbook"
    assert client.patch(f"/api/memory/{row_id}",
                        json={"scope": "housekeeping"}).status_code == 400


# ------------------------------------------------------------------ CLI boundary


def test_cli_add_rejects_and_names_the_allowed_values(initialized):
    for args, expected in (
        (["memory", "add", "x", "--category", "global", "--scope", "environment-notes"],
         "environment|preferences|patterns"),
        (["memory", "add", "x", "--category", "project", "--scope", "logbook",
          "--project", "demo"], "conventions|decisions|gotchas"),
        (["memory", "add", "x", "--category", "role", "--scope", "logbook-writer"],
         "architect|worker|supervisor"),
    ):
        r = runner.invoke(app, args)
        assert r.exit_code == 1, (args, r.output)
        assert "error:" in r.output and expected in r.output
    from orchestrator.services.bootstrap import DEFAULT_GLOBAL_MEMORY

    with session_scope() as s:
        # Nothing was half-stored: only the memory `init` seeds is present.
        assert len(m.list_entries(s)) == len(DEFAULT_GLOBAL_MEMORY)


def test_cli_list_marks_uninjectable_entries_and_leaves_valid_role_rows_alone(initialized):
    with session_scope() as s:
        bad = legacy(s, category="project", scope="ai-livestreaming-avatar",
                     project_slug="ai-livestreaming-avatar", content="Orphan fact.")
        # The counter-examples from the task: these ARE injectable and must not be flagged.
        legacy(s, category="role", scope="worker", content="Small commits.")
        legacy(s, category="role", scope="worker", role="worker", content="Re-test.")
        bad_id, worker_id = bad.id, None
        for e in m.list_entries(s, category="role"):
            if e.content == "Small commits.":
                worker_id = e.id
    r = runner.invoke(app, ["memory", "list"])
    assert r.exit_code == 0
    # Exactly one row is flagged: the two valid role rows are never misflagged.
    assert r.output.count("uninjectable #") == 1
    assert f"uninjectable #{worker_id}" not in r.output
    assert "conventions|decisions|gotchas" in r.output
    assert f"uninjectable #{bad_id} project:" in r.output
    filtered = runner.invoke(app, ["memory", "list", "--uninjectable"])
    assert str(bad_id) in filtered.output and str(worker_id) not in filtered.output
    assert filtered.output.count("uninjectable #") == 1
    warn = [ln for ln in filtered.output.splitlines() if "fix-scopes" in ln]
    assert warn and "cannot be injected" in warn[0]


def test_cli_fix_scopes_prints_the_ids_it_moved_and_rerenders_both_files(initialized):
    root = m.memory_root()
    with session_scope() as s:
        a = legacy(s, category="global", scope="global-environment-verifying-orchestrator",
                   content="Verification is path-independent.", approved=True)
        b = legacy(s, category="project", scope="ai-livestreaming-avatar",
                   project_slug="ai-livestreaming-avatar", content="Repo is reachable.",
                   approved=True)
        legacy(s, category="project", scope="ai-livestreaming-avatar", project_slug="p2",
               content="Rejected, so untouched.", rejected=True)
        ids = f"{a.id}, {b.id}"
    dry = runner.invoke(app, ["memory", "fix-scopes", "--dry-run"])
    assert dry.exit_code == 0
    assert f"would move #{a.id}" in dry.output and f"would move #{b.id}" in dry.output
    assert f"(ids: {ids})" in dry.output
    assert (root / "global" / "global-environment-verifying-orchestrator.md").exists()

    run = runner.invoke(app, ["memory", "fix-scopes"])
    assert run.exit_code == 0, run.output
    assert (
        f"moved #{a.id} global: 'global-environment-verifying-orchestrator' -> 'environment'"
        in run.output
    )
    assert f"(ids: {ids})" in run.output
    assert "left #" in run.output  # the rejected row is reported as left alone
    with session_scope() as s:
        rows = {e.id: e for e in m.list_entries(s)}
        assert rows[a.id].scope == "environment" and rows[b.id].scope == "gotchas"
        assert rows[b.id].approved and not rows[b.id].rejected
    assert (root / "global" / "environment.md").read_text().count(
        "Verification is path-independent."
    ) == 1
    assert "Repo is reachable." in (
        root / "projects" / "ai-livestreaming-avatar" / "gotchas.md"
    ).read_text()
    assert not (root / "global" / "global-environment-verifying-orchestrator.md").exists()
    assert not (
        root / "projects" / "ai-livestreaming-avatar" / "ai-livestreaming-avatar.md"
    ).exists()
    # Idempotent: nothing left to move.
    again = runner.invoke(app, ["memory", "fix-scopes"])
    assert "moved 0 entries" in again.output


# ------------------------------------------------------- reporting honesty (review round)


def test_cli_fix_scopes_reports_lines_the_operator_can_actually_read(initialized):
    """Interpolated text is escaped, and a dry run only ever says "would".

    `[red]error:[/red]` markup is the CLI's own. A scope or a reason containing brackets used
    to be parsed as a style tag and VANISH from the printed line — the reviewer measured the
    skipped-row reason disappearing from the repair output — so every interpolated value goes
    through `rich.markup.escape`. And a collected ghost file is named as a collection exactly
    once, not twice.
    """
    root = m.memory_root()
    with session_scope() as s:
        kept = legacy(s, category="project", scope="[stale]", project_slug="demo",
                      content="Brackets in a scope.", rejected=True)
        moved = legacy(s, category="global", scope="[dim]notes", content="Escaped.",
                       approved=True)
    ghost = str(root / "global" / "dim-notes.md")  # slugified file name, bracketed scope
    dead = root / "global" / "zz-dead.md"  # already-dead ghost, no owning row at all
    dead.write_text("# zz-dead\n")
    assert kept.id and moved.id and dead.exists()

    dry = runner.invoke(app, ["memory", "fix-scopes", "--dry-run"], env=WIDE)
    assert dry.exit_code == 0, dry.output
    d = flat(dry.output)
    assert "scope '[stale]' alone:" in d  # brackets survive
    assert "rejected rows are left alone" in d  # the reason is not eaten
    assert "would move" in d and "would re-render" in d
    # the file that only becomes dead AFTER the move is reported once, as a collection;
    # a file that is already dead is reported once, as a removal
    assert "would collect dead scope-file" in d
    assert f"would remove dead scope-file {ghost}" not in d
    assert f"would remove dead scope-file {dead}" in d
    # a dry run must never claim a write
    for lie in ("moved #", "re-rendered", "removed dead scope-file", " collected "):
        assert lie not in d, lie
    assert "(dry run, nothing written)" in d
    assert Path(ghost).exists() and dead.exists()

    run = runner.invoke(app, ["memory", "fix-scopes"], env=WIDE)
    assert run.exit_code == 0, run.output
    r = flat(run.output)
    assert "would" not in r
    assert r.count("removed dead scope-file") == 2  # both ghosts, one line each
    assert r.count(f"removed dead scope-file {dead}") == 1
    assert r.count(f"removed dead scope-file {ghost}") == 1
    assert "collected" not in r
    assert "'[dim]notes' -> 'environment'" in r  # bracketed scope printed literally
    assert not Path(ghost).exists() and not dead.exists()
    with session_scope() as s:
        assert [e.scope for e in m.list_entries(s) if e.id == moved.id] == ["environment"]


def test_cli_fix_scopes_no_gc_moves_rows_and_deletes_nothing(initialized):
    root = m.memory_root()
    with session_scope() as s:
        row = legacy(s, category="global", scope="logbook", content="Keep the file.",
                     approved=True)
        row_id = row.id
    out = runner.invoke(app, ["memory", "fix-scopes", "--no-gc"], env=WIDE)
    assert out.exit_code == 0, out.output
    assert "gc is off" in out.output
    assert "removed dead scope-file" not in out.output
    assert (root / "global" / "logbook.md").exists()
    # the row moved and its old file was re-rendered; only the deletion was declined
    assert "mem:" not in (root / "global" / "logbook.md").read_text()
    with session_scope() as s:
        assert [e.scope for e in m.list_entries(s) if e.id == row_id] == ["environment"]


def test_cli_list_prints_an_uninjectable_scope_literally(initialized):
    """The `inj` marker has to NAME the bad scope; a swallowed bracket hides the defect."""
    with session_scope() as s:
        legacy(s, category="project", scope="[logbook]", project_slug="demo",
               content="Bracketed scope.")
    out = runner.invoke(app, ["memory", "list"])
    assert out.exit_code == 0, out.output
    assert "uninjectable #" in out.output
    assert "'[logbook]'" in out.output
    assert "conventions|decisions|gotchas" in out.output


def test_cli_import_repairs_a_legacy_export_scope(initialized):
    """`import` REPAIRS; the create boundaries REJECT (docs/memory.md says which is which).

    An export written before validation existed must not lose its facts, while a live
    create/PATCH can still be refused and corrected. Both are true at once, and the CLI
    output shows the repaired scope.
    """
    with session_scope() as s:
        legacy(s, category="project", scope="logbook", project_slug="demo",
               content="A fact worth keeping.")
        payload = json.loads(m.dumps_export(s))
    assert any(e["scope"] == "logbook" for e in payload["entries"])
    exported = initialized / "mem.json"
    exported.write_text(json.dumps(payload))
    first = runner.invoke(app, ["memory", "import", str(exported)])
    assert first.exit_code == 0, first.output
    assert "imported 1" in first.output
    second = runner.invoke(app, ["memory", "import", str(exported)])
    assert "imported 0" in second.output  # idempotent on the repaired scope
    with session_scope() as s:
        imported = [e for e in m.list_entries(s, scope="gotchas") if e.project_slug == "demo"]
        # exactly one copy: the legacy row itself keeps its bad scope until fix-scopes
        assert [e.content for e in imported] == ["A fact worth keeping."]
        assert sorted(e.scope for e in m.list_entries(s, category="project")) == (
            ["gotchas", "logbook"]
        )
    # and the same scope is still refused at the create boundary
    refused = runner.invoke(app, ["memory", "add", "x", "--category", "project",
                                  "--scope", "logbook", "--project", "demo"])
    assert refused.exit_code == 1
    assert "conventions|decisions|gotchas" in refused.output
