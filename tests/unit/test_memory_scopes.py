"""T-4: the scope vocabularies — coercion, boundary rejection, repair, list marker.

Everything here is offline: an isolated ORCHESTRATOR_HOME (the `session` fixture) and no
network, no API keys.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.core.models import MemoryCategory, MemoryEntry, Role
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion, MemoryUpdate
from orchestrator.services import memory_service as m


def legacy(session, *, category, scope, content, project_slug=None, role=None, approved=False,
           rejected=False, source="handoff:R-3"):
    """Insert a row straight into the table, exactly the way the unvalidated suggestion
    path did before T-4, so the broken data under repair is real broken data."""
    entry = MemoryEntry(
        category=category,
        scope=scope,
        project_slug=project_slug,
        role=role,
        content=content,
        source=source,
        approved=approved,
        rejected=rejected,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    if entry.approved:
        m.render_file(session, m._file_for(entry))
    return entry


def suggest(session, *items, project_slug="ai-livestreaming-avatar", role="worker"):
    return m.suggest_from_handoff_report(
        session, [MemorySuggestion(**i) for i in items], "handoff:R-3", project_slug, role=role
    )


# ------------------------------------------------------------------ vocabularies


def test_the_three_vocabularies_are_importable_named_constants():
    assert m.GLOBAL_SCOPES == ("environment", "preferences", "patterns")
    assert m.PROJECT_SCOPES == ("conventions", "decisions", "gotchas")
    # The missing constant this task had to add: role scopes are the role memory FILES.
    assert m.ROLE_SCOPES == m.ROLE_FILES == ("architect", "worker", "supervisor")
    assert m.ROLE_SCOPE_ALIASES == {"reviewer": "architect", "agent": "worker"}
    assert m.SCOPE_VOCABULARY == {
        MemoryCategory.global_: m.GLOBAL_SCOPES,
        MemoryCategory.project: m.PROJECT_SCOPES,
        MemoryCategory.role: m.ROLE_SCOPES,
    }


def test_normalize_scope_applies_the_role_aliases():
    assert m.normalize_scope(MemoryCategory.role, "worker") == "worker"
    assert m.normalize_scope(MemoryCategory.role, "reviewer") == "architect"
    assert m.normalize_scope(MemoryCategory.role, "agent") == "worker"
    assert m.normalize_scope(MemoryCategory.project, "gotchas") == "gotchas"
    with pytest.raises(m.MemoryError_):
        m.normalize_scope(MemoryCategory.project, "logbook")


# ------------------------------------------------------------------ coercion (DoD 1)


def test_project_suggestion_with_an_invented_scope_is_coerced_and_recorded(session):
    out = suggest(
        session,
        {
            "category": "project",
            "scope": "ai-livestreaming-avatar",
            "content": "The repo is reachable directly on this host.",
        },
    )
    assert len(out.created) == 1
    entry = out.created[0]
    assert entry.scope == "gotchas"  # the deterministic fallback
    assert not entry.approved
    # The substitution is recorded for the operator, in words that name both halves.
    assert len(out.notes) == 1
    assert "ai-livestreaming-avatar" in out.notes[0] and "gotchas" in out.notes[0]
    assert out.moved == out.notes and not out.notes[0].startswith("dropped ")
    assert m.is_injectable(entry)
    assert "worker" in m.reachable_roles(entry)


def test_global_suggestion_with_a_scope_that_hints_a_vocabulary_word_falls_back(session):
    """The hint is not followed: `environment` here is the FALLBACK, not an inference."""
    out = suggest(
        session,
        {
            "category": "global",
            "scope": "global-environment-verifying-orchestrator-change",
            "content": "Verification must run in the worktree.",
        },
        project_slug=None,
    )
    assert [e.scope for e in out.created] == [m.FALLBACK_SCOPE[MemoryCategory.global_]]
    assert "deterministic fallback" in out.notes[0]


def test_coercion_falls_back_and_never_infers_from_the_words_in_the_scope(session):
    """DoD 1 read literally: the out-of-vocabulary case has ONE answer per category.

    Inferring `decisions` from a scope that merely mentions it would change WHICH ROLES
    receive the note (decisions -> Architect+Supervisor, gotchas -> Worker+Supervisor), and
    the substring pass used to fire inside unrelated words. Neither is allowed.
    """
    fallbacks = {
        MemoryCategory.project: "gotchas",
        MemoryCategory.global_: "environment",
    }
    for category, expected in fallbacks.items():
        for scope in (
            "decisions-and-gating",  # a vocabulary TOKEN inside a longer scope
            "decisionss",  # a vocabulary word as a raw SUBSTRING
            "conventions-notes",
            "patterns-of-the-team",
            "logbook",
        ):
            got, note = m.coerce_scope(category, scope)
            assert got == expected, (category, scope, got)
            assert note and "deterministic fallback" in note, (category, scope, note)
    # In-vocabulary scopes are untouched and silent.
    assert m.coerce_scope(MemoryCategory.project, "decisions") == ("decisions", "")
    assert m.coerce_scope(MemoryCategory.global_, "patterns") == ("patterns", "")


def test_a_scope_mentioning_decisions_does_not_route_the_note_to_the_architect(session):
    out = suggest(
        session,
        {
            "category": "project",
            "scope": "decisions-about-the-gate",
            "content": "Parse reviews fail closed.",
        },
    )
    entry = out.created[0]
    assert entry.scope == "gotchas"
    assert "worker" in m.reachable_roles(entry)
    assert "architect" not in m.reachable_roles(entry)


def test_suggestions_with_no_hint_fall_back_deterministically(session):
    first = suggest(
        session,
        {"category": "global", "scope": "misc-note", "content": "A fact."},
        project_slug=None,
    )
    assert [e.scope for e in first.created] == [m.FALLBACK_SCOPE[MemoryCategory.global_]]
    second = suggest(
        session,
        {"category": "project", "scope": "logbook", "content": "Another fact."},
    )
    assert [e.scope for e in second.created] == ["gotchas"]
    assert any("fallback" in n for n in second.notes)


def test_role_suggestion_outside_the_vocabulary_is_dropped_not_stored(session):
    out = suggest(
        session,
        {
            "category": "role",
            "scope": "ai-livestreaming-avatar-logbook",
            "content": "The logbook writer keeps a running diary.",
        },
    )
    assert out.created == []
    assert len(out.notes) == 1 and out.notes[0].startswith("dropped ")
    assert "architect|worker|supervisor" in out.notes[0]
    assert m.list_entries(session, category="role") == []


def test_in_vocabulary_suggestions_are_stored_untouched_and_silent(session):
    before = len(m.list_entries(session))
    out = suggest(
        session,
        {"category": "project", "scope": "gotchas", "content": "Mount path is fake."},
        {"category": "global", "scope": "patterns", "content": "Assert the resolution."},
        {"category": "role", "scope": "worker", "content": "One phase at a time."},
    )
    assert [e.scope for e in out.created] == ["gotchas", "patterns", "worker"]
    assert out.notes == []
    assert len(m.list_entries(session)) == before + 3


def test_role_alias_suggestion_lands_in_the_shared_role_file(session):
    out = suggest(
        session,
        {"category": "role", "scope": "reviewer", "content": "Fail closed on parsing."},
    )
    entry = out.created[0]
    assert entry.scope == "architect" and entry.role == "reviewer"
    assert out.notes == []
    m.approve(session, entry.id)
    root = m.memory_root()
    assert "Fail closed on parsing." in (root / "roles" / "architect.md").read_text()
    assert "Fail closed on parsing." in m.for_prompt(session, "reviewer", None)["role"]


def test_coerced_suggestion_reaches_a_prompt_once_approved(session):
    out = suggest(
        session,
        {"category": "project", "scope": "ai-livestreaming-avatar", "content": "Reachable host."},
    )
    entry = out.created[0]
    assert entry.content == "Reachable host." and entry.scope == "gotchas"
    assert "Reachable host." not in m.for_prompt(session, "worker", "ai-livestreaming-avatar")[
        "project"
    ]
    m.approve(session, entry.id)
    assert "Reachable host." in m.for_prompt(session, "worker", "ai-livestreaming-avatar")[
        "project"
    ]
    assert "Reachable host." not in m.for_prompt(session, "architect", "ai-livestreaming-avatar")[
        "project"
    ]


def test_duplicate_suggestions_are_compared_on_the_repaired_scope(session):
    item = {"category": "project", "scope": "ai-livestreaming-avatar", "content": "Same fact."}
    assert len(suggest(session, item).created) == 1
    # The second copy is proposed under the *good* scope name; it must still deduplicate
    # against the row stored under the coerced scope.
    out = suggest(session, {**item, "scope": "misc"})
    assert out.created == []
    assert len(m.list_entries(session, category="project")) == 1


# ------------------------------------------------------------- boundary (DoD 2)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (
            {"category": "global", "scope": "environment-notes-etc", "content": "x"},
            "environment|preferences|patterns",
        ),
        (
            {"category": "project", "scope": "conventions-and-decisions", "content": "x",
             "project_slug": "demo"},
            "conventions|decisions|gotchas",
        ),
        ({"category": "role", "scope": "logbook-writer", "content": "x"}, "architect|worker|supervisor"),
    ],
)
def test_create_rejects_out_of_vocabulary_scope_for_every_category(session, kwargs, expected):
    before = len(m.list_entries(session))
    with pytest.raises(m.MemoryError_) as exc:
        m.create(session, MemoryCreate(**kwargs))
    assert expected in str(exc.value)
    assert len(m.list_entries(session)) == before  # nothing half-stored


@pytest.mark.parametrize("kwargs", [
    {"category": "global", "scope": "preferences", "content": "x"},
    {"category": "project", "scope": "decisions", "content": "x", "project_slug": "demo"},
    {"category": "role", "scope": "supervisor", "content": "x"},
    {"category": "role", "scope": "reviewer", "content": "x", "role": "reviewer"},
])
def test_create_accepts_the_vocabulary(session, kwargs):
    entry = m.create(session, MemoryCreate(**kwargs))
    assert m.is_injectable(entry)


def test_update_rejects_without_touching_the_row_or_the_rendered_file(session):
    """A refused PATCH must not depend on session rollback to be harmless.

    `update()` validates a throwaway preview BEFORE assigning anything, so the stored row
    and its markdown are byte-for-byte unchanged after the error.
    """
    entry = m.create(
        session,
        MemoryCreate(
            category="project",
            scope="gotchas",
            project_slug="demo",
            content="Watch out.",
            approved=True,
        ),
    )
    home = m._file_for(entry)
    before = home.read_text()
    with pytest.raises(m.MemoryError_):
        m.update(session, entry.id, MemoryUpdate(scope="housekeeping", content="Reworded."))
    fresh = session.get(MemoryEntry, entry.id)
    assert fresh.scope == "gotchas" and fresh.content == "Watch out."
    assert home.read_text() == before
    assert not session.dirty and not session.new  # nothing half-assigned waiting for a commit
    # The same call with a good scope still works, and a content-only edit still works.
    m.update(session, entry.id, MemoryUpdate(content="Reworded."))
    assert session.get(MemoryEntry, entry.id).content == "Reworded."
    assert "Reworded." in home.read_text()


def test_update_rejects_an_out_of_vocabulary_scope(session):
    entry = m.create(
        session,
        MemoryCreate(category="project", scope="gotchas", project_slug="demo", content="x"),
    )
    with pytest.raises(m.MemoryError_) as exc:
        m.update(session, entry.id, MemoryUpdate(scope="housekeeping"))
    assert "conventions|decisions|gotchas" in str(exc.value)
    session.refresh(entry)
    assert entry.scope == "gotchas"
    m.update(session, entry.id, MemoryUpdate(scope="decisions"))
    session.refresh(entry)
    assert entry.scope == "decisions"


def test_update_of_a_legacy_row_edits_content_without_freezing_on_its_bad_scope(session):
    """The scope the CALLER supplies is what the boundary rejects.

    Refusing to touch a row only because its STORED scope is legacy-invalid would lock the
    operator out of the rows `fix-scopes` exists to repair.
    """
    bad = legacy(session, category="project", scope="ai-livestreaming-avatar",
                 project_slug="demo", content="Old wording.")
    role = legacy(session, category="role", scope="logbook-writer", content="Who?")
    assert not m.is_injectable(bad) and not m.is_injectable(role)
    m.update(session, bad.id, MemoryUpdate(content="New wording."))
    m.update(session, role.id, MemoryUpdate(content="New role wording."))
    session.refresh(bad)
    session.refresh(role)
    assert bad.content == "New wording." and bad.scope == "ai-livestreaming-avatar"
    assert role.content == "New role wording." and role.scope == "logbook-writer"
    # A supplied scope is still refused, legacy row or not.
    with pytest.raises(m.MemoryError_):
        m.update(session, bad.id, MemoryUpdate(scope="housekeeping"))


# ------------------------------------------ boundary: the ROLE column, not just the scope


def test_check_role_name_names_the_role_vocabulary():
    """The role vocabulary is `ROLE_SCOPES` after `ROLE_MEMORY_KEY`, and it is importable."""
    assert m.check_role_name("worker") == "worker"
    assert m.check_role_name("supervisor") == "supervisor"
    assert m.check_role_name("reviewer") == "reviewer"  # a role NAME, not a scope
    assert m.role_memory_file("reviewer") == "architect"  # ...which resolves to this file
    assert m.role_memory_file("agent") == "worker"
    for bad in ("logbook-writer", "ai-livestreaming-avatar", ""):
        with pytest.raises(m.MemoryError_) as exc:
            m.check_role_name(bad)
        msg = str(exc.value)
        assert "architect|worker|supervisor" in msg, bad
        assert repr(bad) in msg, bad


def test_create_refuses_an_unreadable_role_column_even_when_the_scope_is_valid(session):
    """DoD 2 at the create boundary: `role` is what decides which prompt reads the row.

    `MemoryCreate.role` is typed as the Role enum, so an HTTP caller cannot send
    'logbook-writer' at all (see the API test in test_memory_scope_boundary.py). The schema
    object is built here WITHOUT validation to exercise the service boundary that `import`
    and any non-HTTP caller reach with a raw string -- the exact shape of the logbook-writer
    row that started T-4: a valid `scope` hiding a role no injector can select. Before this
    round that shape died on `AttributeError: 'str' object has no attribute 'value'` inside
    `create()` instead of being refused with the vocabulary.
    """
    body = MemoryCreate.model_construct(
        category=MemoryCategory.role,
        scope="worker",
        content="Diary.",
        role="logbook-writer",
        approved=False,
        tags=[],
        source="handoff:R-3",
        project_slug=None,
    )
    before = len(m.list_entries(session))
    with pytest.raises(m.MemoryError_) as exc:
        m.create(session, body)
    assert "architect|worker|supervisor" in str(exc.value)
    assert "logbook-writer" in str(exc.value)
    assert len(m.list_entries(session)) == before  # nothing half-stored
    assert not (m.memory_root() / "roles" / "logbook-writer.md").exists()


def test_update_refuses_a_legacy_role_column_that_no_prompt_reads(session):
    """DoD 2 at the update boundary: an in-vocabulary scope does not launder a dead role."""
    dead = legacy(session, category="role", scope="worker", role="logbook-writer",
                  content="Diary.", approved=True)
    home = m._file_for(dead)
    before = home.read_text()
    with pytest.raises(m.MemoryError_) as exc:
        m.update(session, dead.id, MemoryUpdate(scope="worker"))
    assert "architect|worker|supervisor" in str(exc.value)
    session.refresh(dead)
    assert dead.role == "logbook-writer" and dead.scope == "worker"
    assert home.read_text() == before  # the refused PATCH wrote nothing; no rollback needed
    assert not session.dirty and not session.new
    # A content-only edit stays possible, so the row is not frozen out of the API.
    m.update(session, dead.id, MemoryUpdate(content="Reworded."))
    session.refresh(dead)
    assert dead.content == "Reworded."


def test_import_refuses_an_unreadable_role_column_without_storing_it(session):
    """The repair path repairs SCOPES; it never stores a role no injector can select."""
    payload = {
        "version": 1,
        "entries": [
            {"category": "role", "scope": "worker", "role": "logbook-writer",
             "content": "Diary.", "approved": True},
        ],
    }
    assert m.import_all(session, payload) == 0
    assert m.list_entries(session, category="role") == []
    assert not (m.memory_root() / "roles" / "logbook-writer.md").exists()


def test_role_and_scope_disagreements_land_on_the_file_the_row_is_read_from(session):
    """`role` wins and `scope` follows, so a row cannot name a file it will not be read in.

    category=role + role='reviewer' + scope='worker': `for_prompt` selects on the `role`
    column (reviewer -> the architect file), so the scope normalizes to 'architect'.
    """
    entry = m.create(
        session,
        MemoryCreate(category="role", scope="worker", role="reviewer", content="Gate."),
    )
    assert entry.role == "reviewer" and entry.scope == "architect"
    assert m._file_for(entry).name == "architect.md"
    assert m.is_injectable(entry)
    assert m.reachable_roles(entry) == ("architect", "reviewer")


# ---------------------------------------------------------------- repair (DoD 3)


def test_fix_scopes_moves_legacy_rows_and_re_renders_both_files(session):
    slug = "ai-livestreaming-avatar"
    stale = legacy(
        session,
        category="project",
        scope=slug,
        project_slug=slug,
        content="The repo is reachable directly on this host.",
        approved=True,
    )
    other = m.create(
        session,
        MemoryCreate(
            category="project", scope="gotchas", project_slug=slug, approved=True,
            content="Keep this line.",
        ),
    )
    root = m.memory_root()
    orphan = root / "projects" / slug / f"{slug}.md"
    home = root / "projects" / slug / "gotchas.md"
    assert f"mem:{stale.id}" in orphan.read_text()  # the rendered orphan from the live DB

    repair = m.fix_scopes(session)
    assert repair.moved_ids == [stale.id]
    assert repair.moved[0]["from"] == slug and repair.moved[0]["to"] == "gotchas"
    session.refresh(stale)
    assert stale.scope == "gotchas"
    # The NEW file was re-rendered and keeps the entry it already owned.
    text = home.read_text()
    assert f"mem:{stale.id}" in text and f"mem:{other.id}" in text
    # The OLD file cannot keep a stale line: it is re-rendered, then collected because it
    # is named after something no injector reads.
    assert not orphan.exists() or f"mem:{stale.id}" not in orphan.read_text()
    assert m.is_injectable(stale)
    # Idempotent: a second run moves nothing.
    again = m.fix_scopes(session)
    assert again.moved == [] and again.skipped == []


def test_fix_scopes_leaves_rejected_rows_alone(session):
    row = legacy(
        session, category="project", scope="ai-livestreaming-avatar", project_slug="p",
        content="Nope.", rejected=True,
    )
    repair = m.fix_scopes(session)
    assert repair.moved == []
    assert [s["id"] for s in repair.skipped] == [row.id]
    assert "rejected" in repair.skipped[0]["why"]
    session.refresh(row)
    assert row.scope == "ai-livestreaming-avatar" and row.rejected


def test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row(session):
    row = legacy(session, category="role", scope="logbook-writer", content="Who?")
    repair = m.fix_scopes(session)
    assert repair.moved == []
    assert [s["id"] for s in repair.skipped] == [row.id]
    assert "role" in repair.skipped[0]["why"]
    assert m.list_entries(session, category="role")  # never deleted, just reported


def test_fix_scopes_reports_a_canonical_scope_that_is_still_unreachable(session):
    row = legacy(session, category="project", scope="gotchas", content="No project.")
    repair = m.fix_scopes(session)
    assert repair.moved == []
    assert [s["id"] for s in repair.skipped] == [row.id]
    assert "project_slug" in repair.skipped[0]["why"]


def test_fix_scopes_dry_run_writes_nothing(session):
    row = legacy(
        session, category="project", scope="ai-livestreaming-avatar", project_slug="p",
        content="X", approved=True,
    )
    orphan = m.memory_root() / "projects" / "p" / "ai-livestreaming-avatar.md"
    root = m.memory_root()
    repair = m.fix_scopes(session, dry_run=True)
    assert repair.moved_ids == [row.id] and repair.dry_run
    session.refresh(row)
    assert row.scope == "ai-livestreaming-avatar"
    assert orphan.exists()
    # A dry run still says what it would do to the rendered files.
    assert repair.moved[0]["old_file"] == str(orphan)
    assert repair.moved[0]["collect_old_file"] is True
    assert repair.moved[0]["new_file"] == str(root / "projects" / "p" / "gotchas.md")
    assert not repair.removed_files and orphan.exists()


def test_gc_removes_dead_files_and_keeps_the_canonical_layout(session):
    root = m.memory_root()
    dead = root / "projects" / "p" / "p.md"
    dead.parent.mkdir(parents=True, exist_ok=True)
    dead.write_text("# p\n")  # the title-only ghost left behind by a hand re-scope
    assert (root / "global" / "patterns.md").exists()  # canonical, empty, from init
    removed = m.gc_orphan_files(session)
    assert removed == [dead]
    assert not dead.exists()
    assert (root / "global" / "patterns.md").exists()
    assert not (root / "projects" / "p").exists()  # emptied project directory tidied away


def test_gc_only_deletes_files_render_file_could_have_written(session):
    """Hand-authored markdown is operator input (docs/memory.md, `import --markdown`).

    The GC predicate is `in_layout_dir AND not canonical_file AND not owned AND
    _renderer_output`, and the last term is what protects a human's file: the first line
    must be exactly `# <stem>` and every later line must be a `- … <!-- mem:N -->` marker.
    """
    root = m.memory_root()
    proj = root / "projects" / "p"
    proj.mkdir(parents=True, exist_ok=True)
    hand = proj / "notes.md"
    hand.write_text("# Hand-written notes\n\n- a line someone typed by hand\n")
    hand_title_only = proj / "scratch.md"  # right heading shape, prose body -> kept
    hand_title_only.write_text("# scratch\n\nremember to check the mount path\n")
    header = proj / "conventions.md"  # canonical name holding an ensure_layout header
    header.write_text("# Project p — conventions\n")
    ghost = proj / "p.md"  # render_file's own output, now with no owning entry
    ghost.write_text("# p\n")
    stale = proj / "old-scope.md"  # rendered with a marker whose entry has moved away
    stale.write_text("# old-scope\n\n- dead line  <!-- mem:999 -->\n")
    outside = root.parent / "zz-p.md"  # not in a layout directory
    outside.write_text("# p\n")

    removed = m.gc_orphan_files(session)
    assert removed == [stale, ghost]  # sorted by path, and only the two renderer-shaped files
    assert hand.exists() and hand_title_only.exists() and header.exists()
    assert not ghost.exists() and not stale.exists()
    assert outside.exists()
    assert m.orphan_files(session) == []  # idempotent


def test_fix_scopes_can_move_rows_without_deleting_a_single_file(session):
    row = legacy(
        session, category="project", scope="ai-livestreaming-avatar", project_slug="p",
        content="Reachable host.", approved=True,
    )
    ghost = m.memory_root() / "projects" / "p" / "ai-livestreaming-avatar.md"
    assert ghost.exists()
    repair = m.fix_scopes(session, gc=False)
    assert repair.moved_ids == [row.id]
    assert repair.removed_files == [] and ghost.exists()
    # `--no-gc` still SHOWS the dead file instead of pretending there is none.
    assert [str(p) for p in repair.ghost_files] == [str(ghost)]
    assert f"mem:{row.id}" not in ghost.read_text()  # re-rendered, just not deleted
    assert f"mem:{row.id}" in (m.memory_root() / "projects" / "p" / "gotchas.md").read_text()


def test_import_does_not_duplicate_a_row_that_is_still_awaiting_fix_scopes(session):
    """The repair must not manufacture a second copy of a fact that is already stored.

    A legacy row keeps its dead scope until `fix-scopes` runs, so the duplicate check takes
    BOTH spellings into account: the stored scope and the scope it would be repaired to.
    """
    legacy(session, category="project", scope="logbook", project_slug="demo",
           content="Same fact.")
    payload = json.loads(m.dumps_export(session))
    assert m.import_all(session, payload) == 0
    rows = [e for e in m.list_entries(session) if e.content == "Same fact."]
    assert len(rows) == 1 and rows[0].scope == "logbook"  # untouched, still repairable
    m.fix_scopes(session)
    assert m.import_all(session, payload) == 0  # and after the repair, still no duplicate
    with pytest.raises(m.MemoryError_):
        m.import_all(session, {"version": 1, "entries": "not a list"})


def test_import_repairs_a_legacy_export_instead_of_dropping_it(session):
    slug = "ai-livestreaming-avatar"
    payload = {
        "version": 1,
        "entries": [
            {"category": "project", "scope": slug, "project_slug": slug,
             "content": "Reachable host.", "approved": False},
            {"category": "role", "scope": "logbook-writer", "content": "Dropped.",
             "approved": False},
        ],
    }
    assert m.import_all(session, payload) == 1  # the repairable one, not the unplaceable one
    rows = m.list_entries(session, category="project")
    assert [r.scope for r in rows if r.source == "import"] == ["gotchas"]
    assert m.import_all(session, payload) == 0  # idempotent on the repaired values


# ------------------------------------------------------------- list marker (DoD 4)


def test_role_rows_still_render_by_the_rule_main_used(session):
    """T-4 changes no rendering and no role-injection semantics (frozen contract).

    A legacy row whose `role` is not a role memory file keeps rendering to its own dead
    file — it is REPORTED as uninjectable, not quietly re-homed into the scope's file — and
    the rows that always worked keep working.
    """
    root = m.memory_root()
    dead_role = legacy(
        session, category="role", scope="worker", role="logbook-writer",
        content="Diary of the logbook writer.", approved=True,
    )
    assert m._file_for(dead_role).name == "logbook-writer.md"
    assert (root / "roles" / "logbook-writer.md").exists()
    assert "Diary of the logbook writer." not in (root / "roles" / "worker.md").read_text()
    assert not m.is_injectable(dead_role)
    assert "logbook-writer.md" in m.injectability(dead_role)[1]
    # fix-scopes refuses to invent a role, so this row is reported and stays put.
    repair = m.fix_scopes(session)
    assert [s["id"] for s in repair.skipped] == [dead_role.id]
    session.refresh(dead_role)
    assert dead_role.role == "logbook-writer" and dead_role.scope == "worker"

    # The counter-examples from the task (#39/#47): role=worker scope=worker.
    good = legacy(
        session, category="role", scope="worker", role="worker", content="Small commits.",
        approved=True,
    )
    assert m._file_for(good).name == "worker.md" and m.is_injectable(good)
    assert m.reachable_roles(good) == ("worker", "agent")
    assert "Small commits." in m.for_prompt(session, "agent", None)["role"]
    # `scope` is the fallback for the file name exactly as it was on main.
    scope_only = legacy(session, category="role", scope="supervisor", content="Recommend only.")
    assert m._file_for(scope_only).name == "supervisor.md"
    assert m.is_injectable(scope_only)
    # And the boundary refuses such a row going forward, so it cannot be created again.
    with pytest.raises(m.MemoryError_):
        m.create(session, MemoryCreate(category="role", scope="logbook-writer", content="x"))


def test_injectability_marks_only_the_unreachable(session):
    slug = "ai-livestreaming-avatar"
    bad = legacy(session, category="global", scope=slug, content="Global orphan.")
    ok_global = legacy(session, category="global", scope="environment", content="Env fact.")
    bad_project = legacy(
        session, category="project", scope=slug, project_slug=slug, content="Project orphan."
    )
    # The counter-examples from the task: category=role scope=worker IS injectable.
    worker = legacy(session, category="role", scope="worker", content="Small commits.")
    worker_with_role = legacy(
        session, category="role", scope="worker", role="worker", content="Re-test then resubmit."
    )
    reviewer = legacy(session, category="role", scope="architect", role="reviewer", content="Gate.")
    agent = legacy(session, category="role", scope="agent", role="agent", content="Ship one phase.")
    supervisor = legacy(session, category="role", scope="supervisor", content="Recommend only.")

    assert not m.is_injectable(bad) and "environment|preferences|patterns" in m.injectability(bad)[1]
    assert not m.is_injectable(bad_project)
    assert m.is_injectable(ok_global)
    assert m.reachable_roles(ok_global) == tuple(r.value for r in Role)
    for entry in (worker, worker_with_role, reviewer, agent, supervisor):
        assert m.is_injectable(entry), (entry.role, entry.scope)
    assert m.reachable_roles(reviewer) == ("architect", "reviewer")
    assert m.reachable_roles(agent) == ("worker", "agent")


def test_coercion_is_written_to_the_build_log_the_operator_reads(session):
    """DoD 1: the substitution is recorded where the run's log shows it.

    `run_service._store_memory_suggestions` is the seam every handoff path goes through
    (clean exit, salvage, agent run); the notes come back with the entries and become
    build-log events instead of a silent `continue`.
    """
    from sqlmodel import select

    from orchestrator.core.models import BuildLogEntry
    from orchestrator.services import run_service

    out = run_service._store_memory_suggestions(
        session,
        [
            MemorySuggestion(
                category="project", scope="ai-livestreaming-avatar", content="Reachable host."
            ),
            MemorySuggestion(category="role", scope="logbook-writer", content="Diary."),
        ],
        source="handoff:R-3",
        project_slug="ai-livestreaming-avatar",
        role="worker",
    )
    assert [e.scope for e in out.created] == ["gotchas"]
    events = {e.event: e.details for e in session.exec(select(BuildLogEntry))}
    assert "memory_scope_substituted" in events
    assert "ai-livestreaming-avatar" in events["memory_scope_substituted"]
    assert "gotchas" in events["memory_scope_substituted"]
    assert "memory_suggestion_dropped" in events
    assert "architect|worker|supervisor" in events["memory_suggestion_dropped"]


def test_legacy_rows_that_cannot_be_injected_do_not_reach_any_prompt(session):
    slug = "ai-livestreaming-avatar"
    orphan = legacy(
        session, category="project", scope=slug, project_slug=slug,
        content="Invisible to every prompt.", approved=True,
    )
    assert not m.is_injectable(orphan)
    for role in ("worker", "architect", "supervisor", "reviewer", "agent"):
        assert "Invisible to every prompt." not in m.for_prompt(session, role, slug)[ "project"]
    repair = m.fix_scopes(session)
    assert repair.moved_ids == [orphan.id]
    assert "Invisible to every prompt." in m.for_prompt(session, "worker", slug)["project"]
