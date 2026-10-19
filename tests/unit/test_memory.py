import pytest

from orchestrator.core.schemas import MemoryCreate, MemorySuggestion, MemoryUpdate
from orchestrator.services import memory_service


def test_suggestion_approval_flow_and_injection(session):
    created = memory_service.suggest_from_handoff(
        session,
        [
            MemorySuggestion(category="project", scope="gotchas", content="Run migrations first."),
            MemorySuggestion(category="role", scope="worker", content="Small commits."),
        ],
        source="handoff:R-1",
        project_slug="demo",
    )
    assert len(created) == 2 and not any(e.approved for e in created)
    # Unapproved memory is never injected.
    assert (
        "Run migrations first."
        not in memory_service.for_prompt(session, "worker", "demo")["project"]
    )

    gotcha, worker_note = created
    memory_service.approve(session, gotcha.id)
    memory_service.reject(session, worker_note.id)
    mem = memory_service.for_prompt(session, "worker", "demo")
    assert "Run migrations first." in mem["project"]
    assert "Small commits." not in mem["role"]
    # Rendered to markdown under memory/projects/<slug>/gotchas.md
    md = memory_service.memory_root() / "projects" / "demo" / "gotchas.md"
    assert "Run migrations first." in md.read_text()
    assert memory_service.pending_count(session) == 0


def test_role_aware_injection(session):
    def add(**kw):
        return memory_service.create(session, MemoryCreate(approved=True, **kw))

    add(category="project", scope="decisions", project_slug="p", content="Use SQLite.")
    add(category="project", scope="conventions", project_slug="p", content="snake_case.")
    add(category="project", scope="gotchas", project_slug="p", content="Flaky test X.")
    add(category="role", scope="architect", role="architect", content="Approve rarely.")
    arch = memory_service.for_prompt(session, "architect", "p")
    worker = memory_service.for_prompt(session, "worker", "p")
    sup = memory_service.for_prompt(session, "supervisor", "p")
    assert "Use SQLite." in arch["project"] and "snake_case." in arch["project"]
    assert "Flaky test X." not in arch["project"]
    assert "snake_case." in worker["project"] and "Flaky test X." in worker["project"]
    assert "Use SQLite." not in worker["project"]
    assert "Use SQLite." in sup["project"] and "Flaky test X." in sup["project"]
    assert "Approve rarely." in arch["role"] and not worker["role"]
    # Global memory (seeded by init) goes to every role.
    assert "CachyOS" in arch["global"] and "CachyOS" in worker["global"]


def test_duplicate_suggestions_are_ignored(session):
    s = [MemorySuggestion(category="global", scope="patterns", content="Same thing.")]
    assert len(memory_service.suggest_from_handoff(session, s, "a", None)) == 1
    assert memory_service.suggest_from_handoff(session, s, "b", None) == []


def test_search_update_export_import_roundtrip(session):
    e = memory_service.create(
        session,
        MemoryCreate(
            category="global", scope="patterns", content="Prefer argv lists.", approved=True
        ),
    )
    assert [x.id for x in memory_service.search(session, "argv")] == [e.id]
    memory_service.update(session, e.id, MemoryUpdate(content="Prefer argv lists over shells."))
    data = memory_service.export_all(session)
    assert any(x["content"] == "Prefer argv lists over shells." for x in data["entries"])
    assert memory_service.import_all(session, data) == 0  # all duplicates
    data["entries"].append(
        {"category": "global", "scope": "patterns", "content": "New one.", "approved": False}
    )
    assert memory_service.import_all(session, data) == 1
    with pytest.raises(memory_service.MemoryError_):
        memory_service.import_all(session, {"nope": 1})


def test_secrets_are_redacted_from_memory(session):
    e = memory_service.create(
        session,
        MemoryCreate(
            category="global",
            scope="environment",
            content="key is sk-ant-api03-abcdefghijklmnopqrstu",
        ),
    )
    assert "sk-ant-api03" not in e.content


def test_project_memory_requires_slug(session):
    with pytest.raises(memory_service.MemoryError_):
        memory_service.create(
            session, MemoryCreate(category="project", scope="decisions", content="x")
        )
