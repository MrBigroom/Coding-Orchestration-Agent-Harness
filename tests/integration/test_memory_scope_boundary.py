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


def test_api_create_refuses_an_unknown_role_with_a_message_naming_the_role_files(client):
    """DoD 2 at the POST boundary, asserted on the ERROR TEXT and not on the status code.

    The `role` COLUMN is what `for_prompt` selects on, so `role="logbook-writer"` is the
    damaging shape. `MemoryCreate.role` is the `Role` enum, so Pydantic used to answer with
    `Input should be 'architect', 'supervisor', 'worker', 'reviewer' or 'agent'` — role VALUES
    with no mention of the role memory FILES, and no mention of the `reviewer -> architect` /
    `agent -> worker` aliases that make those extra values legal. `MemoryCreate` now runs the
    service's own `check_role_name` in a `model_validator(mode="before")`, which fires before
    the enum coercion, so the body carries the same words the CLI and the service use. The
    status code stays 422 (the value never reaches the service, so nothing can be half-stored)
    and the body keeps the frozen {error, detail} shape. `role` stays typed as `Role`: the
    request shape and the accepted set are untouched, only the rejection TEXT changed.
    """
    r = client.post(
        "/api/memory",
        json={"category": "role", "scope": "worker", "role": "logbook-writer",
              "content": "Diary."},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert set(body) == {"error", "detail"}  # the frozen error shape
    # `detail` is a list of error dicts; read the whole body as whitespace-normalized prose so
    # the assertions are about the WORDS, not the JSON layout or the line wrapping.
    text = flat(" ".join(str(x) for x in body["detail"]) + " " + str(body["error"]))
    # The load-bearing assertions are these three, and they are what Pydantic's own enum
    # message does NOT satisfy: it lists the role VALUES (`'architect', 'supervisor',
    # 'worker', 'reviewer' or 'agent'`), which would pass a per-value loop while still naming
    # no vocabulary and no alias. Verified by mutation: with the `MemoryCreate` validator
    # neutered, only these three fail (see verify-evidence/T-4/README.md, R4-D2).
    assert "architect|worker|supervisor" in text  # the vocabulary, as one string
    assert "allowed role memory files" in text  # FILES, not enum values
    assert "reviewer -> architect" in text and "agent -> worker" in text
    assert "logbook-writer" in text  # and the refused value itself
    # Each allowed file appears, checked as the joined vocabulary rather than as loose words.
    assert "|".join(m.ROLE_SCOPES) in text
    # Nothing was stored, and no dead file was created.
    with session_scope() as s:
        assert [e for e in m.list_entries(s, category="role")
                if e.role == "logbook-writer"] == []
    assert not (m.memory_root() / "roles" / "logbook-writer.md").exists()


def test_api_create_still_accepts_every_role_name_the_enum_allows(client):
    """The message got sharper; the ACCEPTED SET did not change (frozen request shape).

    All five `Role` members are still valid at POST, the aliases included, and each lands in
    the role memory file its prompt actually reads — `reviewer`/`agent` resolve through
    `ROLE_MEMORY_KEY`, so a row is never stored under a file no injector opens.
    """
    expected = {
        "architect": "architect", "reviewer": "architect",
        "worker": "worker", "agent": "worker", "supervisor": "supervisor",
    }
    for name, home in expected.items():
        r = client.post(
            "/api/memory",
            json={"category": "role", "scope": name, "role": name, "content": f"R-{name}."},
        )
        assert r.status_code == 201, (name, r.text)
        assert r.json()["scope"] == home, name
        assert r.json()["role"] == name, name
        assert (m.memory_root() / "roles" / f"{home}.md").exists()
    # and the one value the enum has never accepted is the one refused by name, above
    assert client.post(
        "/api/memory",
        json={"category": "role", "scope": "worker", "role": "logbook-writer",
              "content": "Diary."},
    ).status_code == 422


def test_api_rejects_every_category_with_its_own_named_vocabulary(client):
    """DoD 2 as ONE table: each category's rejection names exactly its own allowed values.

    The three vocabularies are asserted from the constants the tests import, so a vocabulary
    that silently loses a member fails here instead of in the prose of a handoff.
    """
    cases = [
        ({"category": "global", "scope": "global-environment-verifying", "content": "x"},
         m.GLOBAL_SCOPES),
        ({"category": "project", "scope": "ai-livestreaming-avatar-logbook",
          "project_slug": "demo", "content": "x"}, m.PROJECT_SCOPES),
        ({"category": "role", "scope": "logbook-writer", "content": "x"}, m.ROLE_SCOPES),
    ]
    for body, vocab in cases:
        r = client.post("/api/memory", json=body)
        assert r.status_code == 400, (body, r.text)
        err = r.json()["error"]
        assert "|".join(vocab) in err, (vocab, err)
        for value in vocab:
            assert value in err, value
        assert body["scope"] in err  # names the offending value too
        assert client.get("/api/memory", params={"scope": body["scope"]}).json() == []


def test_api_patch_refuses_a_legacy_role_column_even_with_an_in_vocabulary_scope(client):
    """DoD 2 at the PATCH boundary for a row already stored in the dead shape.

    scope='worker' is in the vocabulary; the STORED role is what no prompt can select, and
    `for_prompt`/`_file_for` key off the role column first -- so the PATCH is refused and the
    row, its rendered file and its content-only editability are all untouched.
    """
    with session_scope() as s:
        row = legacy(s, category="role", scope="worker", role="logbook-writer",
                     content="Diary.", approved=True)
        row_id, home = row.id, m._file_for(row)
    before = home.read_text()
    r = client.patch(f"/api/memory/{row_id}", json={"scope": "worker"})
    assert r.status_code == 400, r.json()
    assert "architect|worker|supervisor" in r.json()["error"]
    assert "logbook-writer" in r.json()["error"]
    got = [e for e in client.get("/api/memory").json() if e["id"] == row_id][0]
    assert got["role"] == "logbook-writer" and got["scope"] == "worker"
    assert home.read_text() == before
    # ...and the operator is not locked out of the row: a content edit still goes through.
    ok = client.patch(f"/api/memory/{row_id}", json={"content": "Reworded."})
    assert ok.status_code == 200 and ok.json()["content"] == "Reworded."


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


def test_cli_add_refuses_a_role_name_no_prompt_can_read(initialized):
    """`memory add --role` is a plain string, so its error must name the role FILES.

    Without the pre-check Pydantic answers with `Input should be 'architect', 'supervisor',
    'worker', 'reviewer' or 'agent'` -- the enum, not the memory vocabulary with the
    reviewer -> architect / agent -> worker aliases resolved (DoD 2).
    """
    from orchestrator.services.bootstrap import DEFAULT_GLOBAL_MEMORY

    with session_scope() as s:
        before = len(m.list_entries(s))
        assert before == len(DEFAULT_GLOBAL_MEMORY)
    bad = runner.invoke(
        app,
        ["memory", "add", "Diary.", "--category", "role", "--scope", "worker",
         "--role", "logbook-writer"],
        env=WIDE,
    )
    assert bad.exit_code == 1, bad.output
    assert "error:" in bad.output
    assert "architect|worker|supervisor" in flat(bad.output)
    assert "logbook-writer" in flat(bad.output)
    assert "reviewer -> architect" in flat(bad.output)  # the aliases are spelled out
    with session_scope() as s:
        assert len(m.list_entries(s)) == before  # nothing half-stored
    # A real role name (including an alias) is still accepted, and lands in the file it is
    # read from -- role='reviewer' resolves to the architect file.
    ok = runner.invoke(
        app,
        ["memory", "add", "Gate the merge.", "--category", "role", "--scope", "reviewer",
         "--role", "reviewer"],
        env=WIDE,
    )
    assert ok.exit_code == 0, ok.output
    with session_scope() as s:
        row = [e for e in m.list_entries(s, category="role") if e.content == "Gate the merge."]
        assert len(row) == 1 and row[0].scope == "architect" and row[0].role == "reviewer"
        assert m.is_injectable(row[0])
    assert "Gate the merge." not in (
        m.memory_root() / "roles" / "worker.md").read_text()


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


def test_cli_fix_scopes_gc_only_reports_only_the_files_it_actually_deleted(initialized, monkeypatch):
    """DoD 3: a repair report may claim nothing that did not happen on disk.

    The `--gc-only` branch used to fall back to the pre-GC listing
    (`removed_files=removed or listed`), so whenever the collector returned an empty list the
    CLI still printed `removed dead scope-file <path>` for a file that was sitting right there.
    The report now carries exactly what `gc_orphan_files` unlinked, and nothing else.
    """
    root = m.memory_root()
    ghost = root / "projects" / "demo" / "demo.md"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_text("# demo\n")  # the measured orphan shape: title line only
    with session_scope() as s:
        assert ghost in m.orphan_files(s)

    # Simulate a GC that deletes nothing (a file re-claimed by another row, a failed unlink
    # swallowed upstream): the listing is non-empty, the deletions are not.
    monkeypatch.setattr(m, "gc_orphan_files", lambda *a, **k: [])
    out = runner.invoke(app, ["memory", "fix-scopes", "--gc-only"], env=WIDE)
    assert out.exit_code == 0, out.output
    d = flat(out.output)
    assert "removed dead scope-file" not in d  # the false claim, gone
    assert "0 dead scope-file(s) removed" in d
    assert ghost.exists()  # and the disk agrees with the report

    # A --gc-only --dry-run still shows the operator what a real run would collect.
    dry = flat(runner.invoke(app, ["memory", "fix-scopes", "--gc-only", "--dry-run"],
                             env=WIDE).output)
    assert f"would remove dead scope-file {ghost}" in dry
    assert "1 dead scope-file(s) to remove" in dry
    assert "removed dead scope-file" not in dry
    assert ghost.exists()


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
    create/PATCH can still be refused and corrected. Both are true at once, and a legacy row
    that is ALREADY stored under its dead scope is not duplicated by importing its export.
    """
    payload = {
        "version": 1,
        "entries": [
            {"category": "project", "scope": "logbook", "project_slug": "demo",
             "content": "A fact worth keeping.", "approved": False, "source": "handoff:R-3"},
            {"category": "role", "scope": "logbook-writer", "content": "Unplaceable.",
             "approved": False},
        ],
    }
    exported = initialized / "mem.json"
    exported.write_text(json.dumps(payload))
    assert "imported 1" in runner.invoke(app, ["memory", "import", str(exported)]).output
    assert "imported 0" in runner.invoke(app, ["memory", "import", str(exported)]).output
    with session_scope() as s:
        rows = m.list_entries(s, category="project")
        assert [(e.scope, e.project_slug) for e in rows] == [("gotchas", "demo")]
        assert m.list_entries(s, category="role") == []  # the unplaceable one was skipped
    # a legacy row still stored under its dead scope is the SAME fact, not a new one
    with session_scope() as s:
        legacy(s, category="global", scope="logbook", content="Already stored, badly scoped.")
        again = json.loads(m.dumps_export(s))
    round2 = initialized / "mem2.json"
    round2.write_text(json.dumps(again))
    assert "imported 0" in runner.invoke(app, ["memory", "import", str(round2)]).output
    with session_scope() as s:
        same = [e for e in m.list_entries(s) if e.content == "Already stored, badly scoped."]
        assert len(same) == 1 and same[0].scope == "logbook"  # untouched, awaiting fix-scopes
    # and the same scope is still refused at the create boundary
    refused = runner.invoke(app, ["memory", "add", "x", "--category", "project",
                                  "--scope", "logbook", "--project", "demo"])
    assert refused.exit_code == 1
    assert "conventions|decisions|gotchas" in refused.output
