"""T-4: the scope vocabularies — coercion, boundary rejection, repair, list marker.

Everything here is offline: an isolated ORCHESTRATOR_HOME (the `session` fixture) and no
network, no API keys.
"""

from __future__ import annotations

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


def test_global_suggestion_infers_the_scope_it_names(session):
    out = suggest(
        session,
        {
            "category": "global",
            "scope": "global-environment-verifying-orchestrator-change",
            "content": "Verification must run in the worktree.",
        },
        project_slug=None,
    )
    assert [e.scope for e in out.created] == ["environment"]
    assert "environment" in out.notes[0]


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
