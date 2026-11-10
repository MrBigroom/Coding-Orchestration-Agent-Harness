"""T-17: memory ingest dedup, value-ordered demotion, condense and the redaction record.

All checks are offline: isolated ORCHESTRATOR_HOME, no network, no API keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core import config as config_mod
from orchestrator.core.models import MemoryEntry
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion, MemoryUpdate
from orchestrator.services import memory_service as m
from orchestrator.services import memory_triage as mt
from orchestrator.utils.memory_text import (
    build_df,
    build_term_df,
    duplicate_detail,
    duplicate_reason,
    is_duplicate_pair,
    is_subject_pair,
)
from orchestrator.utils.redaction import REDACTED, redact
from orchestrator.utils.truncate import estimate_tokens


@pytest.fixture
def budgets(monkeypatch):
    def _set(**values: int | bool):
        for key, val in values.items():
            value = str(val).lower() if isinstance(val, bool) else str(val)
            monkeypatch.setenv(f"ORCHESTRATOR_{key.upper()}", value)
        config_mod.reset_settings_cache()

    _set()
    yield _set
    config_mod.reset_settings_cache()


def add(
    session,
    content,
    *,
    category="project",
    scope="gotchas",
    slug="demo",
    approved=True,
    tags=None,
):
    return m.create(
        session,
        MemoryCreate(
            category=category,
            scope=scope,
            content=content,
            project_slug=slug,
            approved=approved,
            tags=tags or [],
        ),
    )


def tokens(entry) -> int:
    return estimate_tokens(f"- {entry.content.strip()}\n")


def rows_snapshot(session):
    return [
        (e.id, e.approved, e.rejected, e.scope, e.content, tuple(e.tags_json or []))
        for e in m.list_entries(session)
    ]


# ---------------------------------------------------------------- ingest dedup (DoD 1)

ANCHOR_RULE = (
    "Redaction must use a negative lookbehind anchor before the sk- credential prefix: an "
    "unanchored pattern matches inside words that end in sk and corrupts branch names in "
    "operator-facing text."
)
ANCHOR_RESTATED = (
    "The redactor has to anchor the sk- credential pattern with a negative lookbehind, "
    "because an unanchored pattern matches inside words ending in sk and corrupts branch "
    "names in operator-facing text."
)
DIRTY_PREDICATE = (
    "Two dirty predicates exist deliberately: tracked_dirty gates merges, is_dirty guards "
    "worktree removal, because untracked output must never be discarded."
)
REVIEW_PAYLOAD = (
    "The reviewer prompt and payload are archived to review_payload.md so findings can be "
    "audited after the round and truncated diffs are visible."
)


def test_a_reworded_restatement_is_recorded_as_a_linked_row(session):
    approved = add(session, ANCHOR_RULE)
    # The corpus the service will use: every stored row plus the incoming suggestion.
    df = build_df([e.content for e in m.list_entries(session)] + [ANCHOR_RESTATED])
    is_dup, ratio, threshold = duplicate_detail(ANCHOR_RESTATED, ANCHOR_RULE, df)
    assert is_dup is True

    out = m.suggest_from_handoff(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=ANCHOR_RESTATED)],
        source="handoff:R-9",
        project_slug="demo",
        report=True,
    )
    assert out.created == []
    assert len(out.linked) == 1
    record = out.linked[0]
    assert record["existing_id"] == approved.id
    # The restatement is a REAL pending row: its content is persisted, not only logged.
    linked = session.get(MemoryEntry, record["entry_id"])
    assert linked is not None
    assert linked.approved is False
    assert linked.rejected is False
    assert linked.content == ANCHOR_RESTATED
    # ... recorded as linked to the approved entry it restates, in the existing tags column.
    assert m.duplicate_of(linked) == approved.id
    # The reason names the matcher's own numbers, so a threshold change cannot leave prose behind.
    assert record["reason"] == duplicate_reason(ratio, threshold)
    assert f"#{approved.id}" in out.notes[0]
    assert f"#{linked.id}" in out.notes[0]


def test_a_byte_identical_suggestion_still_creates_no_row(session):
    add(session, ANCHOR_RULE)
    before = len(m.list_entries(session))
    out = m.suggest_from_handoff_report(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=ANCHOR_RULE)],
        source="handoff:R-10",
        project_slug="demo",
    )
    assert out.created == []
    assert out.linked == []
    assert len(m.list_entries(session)) == before


def test_same_domain_but_unrelated_facts_are_not_linked(session):
    first = m.suggest_from_handoff_report(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=DIRTY_PREDICATE)],
        source="handoff:R-11",
        project_slug="demo",
    )
    second = m.suggest_from_handoff_report(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=REVIEW_PAYLOAD)],
        source="handoff:R-12",
        project_slug="demo",
    )
    assert len(first.created) == 1
    assert len(second.created) == 1
    assert second.linked == []


SUBJECT_ANCHOR = (
    "aardvark basilisk capybara dingo emu flamingo gecko `prompt_builder._floor_only_reason` "
    "must compare `max(per_file, cap)`, never per_file alone, or the reason contradicts itself "
    "and the reviewer calls the allocator unverifiable."
)
SUBJECT_RESTATED = (
    "heron ibis jacamar kestrel lemur meerkat narwhal `prompt_builder._floor_only_reason` "
    "compares `max(per_file, cap)`; a per_file-only check prints numbers the reader cannot trust."
)


def test_a_reworded_restatement_shared_only_by_subject_is_linked(session):
    """Rare-word containment alone misses the operator-cited topical restatements.

    Measured on the live pending pile: the evidence-freshness cluster and the diff-floor
    cluster share their subject (code symbols, flags) but not enough rare prose words. The
    second ingest signal must still record the link -- and still never a whole-text ratio.
    """
    approved = m.create(
        session,
        MemoryCreate(
            category="project",
            scope="gotchas",
            project_slug="demo",
            content=SUBJECT_ANCHOR,
            approved=True,
        ),
    )
    df = build_df([e.content for e in m.list_entries(session)] + [SUBJECT_RESTATED])
    tdf = build_term_df([e.content for e in m.list_entries(session)] + [SUBJECT_RESTATED])
    assert is_duplicate_pair(SUBJECT_RESTATED, SUBJECT_ANCHOR, df)[0] is False
    same_subject, shared = is_subject_pair(SUBJECT_RESTATED, SUBJECT_ANCHOR, tdf)
    assert same_subject is True
    assert len(shared) >= 2

    out = m.suggest_from_handoff_report(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=SUBJECT_RESTATED)],
        source="handoff:R-13",
        project_slug="demo",
    )
    assert out.created == []
    assert out.linked[0]["existing_id"] == approved.id
    assert "rare identifier" in out.linked[0]["reason"]


def test_a_repeated_restatement_does_not_add_a_second_linked_row(session):
    add(session, ANCHOR_RULE)
    first = m.suggest_from_handoff_report(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=ANCHOR_RESTATED)],
        source="handoff:R-14",
        project_slug="demo",
    )
    assert len(first.linked) == 1
    before = len(m.list_entries(session))
    second = m.suggest_from_handoff_report(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=ANCHOR_RESTATED)],
        source="handoff:R-15",
        project_slug="demo",
    )
    # The second copy is byte-identical to the already-recorded restatement: no second row,
    # and no second link either (the no-row rule is what the DoD pins).
    assert second.created == []
    assert second.linked == []
    assert len(m.list_entries(session)) == before


def test_a_pending_row_from_the_same_batch_is_not_a_link_target(session):
    """Only an APPROVED fact is a link target (T-17 review round 3, finding 3).

    The first suggestion is stored pending; the second restates it. Linking the second to
    the first would record "linked to existing memory #X" for an unapproved #X, so both are
    stored as ordinary pending rows and out.linked stays empty.
    """
    out = m.suggest_from_handoff_report(
        session,
        [
            MemorySuggestion(category="project", scope="gotchas", content=ANCHOR_RULE),
            MemorySuggestion(category="project", scope="gotchas", content=ANCHOR_RESTATED),
        ],
        source="handoff:R-17",
        project_slug="demo",
    )
    assert out.linked == []
    assert len(out.created) == 2
    assert all(e.approved is False for e in out.created)


# ------------------------------------------------------- value-ordered demotion (DoD 2)


def test_a_pinned_entry_is_kept_while_an_older_unpinned_entry_is_demoted(session, budgets):
    old = add(session, "Old rule " + "alpha " * 10)
    pinned = add(session, "Pinned rule " + "beta " * 10, tags=["pinned"])
    newest = add(session, "Newest rule " + "gamma " * 10)
    budgets(max_memory_tokens_project=tokens(pinned) + tokens(newest) + 1)

    sel = m.select_for_prompt(session, "worker", "demo")
    excluded = {x["id"] for x in sel.excluded}
    assert pinned.content[:20] in sel.sections["project"]
    assert old.id in excluded
    assert pinned.id not in excluded


def test_untagged_entries_keep_the_pre_t17_demotion_order(session, budgets):
    older = add(session, "Older durable rule " + "alpha " * 8)
    newer = add(session, "Newer durable rule " + "beta " * 8)
    stale = add(session, "T-99 decisions: (1) rot-prone newest entry, (2) still task-shaped.")
    budgets(max_memory_tokens_project=tokens(older) + tokens(newer) + 1)

    sel = m.select_for_prompt(session, "worker", "demo")
    excluded = {x["id"]: x for x in sel.excluded}
    assert stale.id in excluded
    assert "rot-prone" in excluded[stale.id]["reason"]
    assert older.id not in excluded


def test_untagged_durable_entries_are_still_demoted_oldest_first(session, budgets):
    older = add(session, "Older durable rule " + "alpha " * 8)
    newer = add(session, "Newer durable rule " + "beta " * 8)
    budgets(max_memory_tokens_project=tokens(newer))

    sel = m.select_for_prompt(session, "worker", "demo")
    excluded = {x["id"]: x for x in sel.excluded}
    assert set(excluded) == {older.id}
    assert newer.content[:20] in sel.sections["project"]
    assert "id #" + str(older.id) + " is only the last tiebreak" in excluded[older.id]["reason"]


def test_two_selections_are_byte_identical_and_exclude_the_same_ids(session, budgets):
    for i in range(5):
        add(session, f"Rule {i}: " + "delta " * 10 + f"resolve the interpreter in worktree {i}.")
    budgets(max_memory_tokens_project=35)
    first = m.select_for_prompt(session, "worker", "demo")
    second = m.select_for_prompt(session, "worker", "demo")
    assert first.sections == second.sections
    assert [x["id"] for x in first.excluded] == [x["id"] for x in second.excluded]


# ------------------------------------------------------------- condense path (DoD 3)


def _condense_fixture(session):
    a = add(
        session,
        "The wrapper must resolve the interpreter inside the worktree before running tests.",
        tags=["tier:3"],
    )
    b = add(
        session,
        "Resolve the interpreter inside the worktree before running tests with the wrapper.",
        tags=["tier:1"],
    )
    c = add(session, "The scheduler coalesces notifications in one pending flush batch.")
    return a, b, c


def test_condense_dry_run_changes_no_row(session):
    _condense_fixture(session)
    before = rows_snapshot(session)
    proposals = m.condense(session, "demo")["proposals"]
    assert proposals
    assert rows_snapshot(session) == before


def test_condense_apply_updates_the_survivor_and_rejects_the_absorbed(session):
    a, b, c = _condense_fixture(session)
    proposal = next(p for p in m.condense(session, "demo")["proposals"] if b.id in p["ids"])
    assert proposal["survivor"] == b.id  # tier:1 outranks tier:3
    result = m.condense(session, "demo", apply_ids=proposal["ids"])
    assert result["applied"][0]["survivor"] == b.id
    session.expire_all()
    assert session.get(MemoryEntry, a.id).rejected is True
    assert session.get(MemoryEntry, b.id).approved is True


def test_condense_partial_apply_is_refused_without_writing(session):
    _condense_fixture(session)
    proposal = m.condense(session, "demo")["proposals"][0]
    before = rows_snapshot(session)
    with pytest.raises(m.MemoryError_):
        m.condense(session, "demo", apply_ids=[proposal["survivor"]])
    assert rows_snapshot(session) == before


def test_condense_rendered_markdown_matches_the_surviving_approved_rows(session):
    _condense_fixture(session)
    proposal = m.condense(session, "demo")["proposals"][0]
    m.condense(session, "demo", apply_ids=proposal["ids"])

    approved = m.list_entries(session, status="approved", project_slug="demo")
    files = {m._file_for(e) for e in approved}
    for path in files:
        entries = sorted(
            (e for e in approved if m._file_for(e) == path), key=lambda e: e.id or 0
        )
        body = "\n".join(f"- {e.content.strip()}  <!-- mem:{e.id} -->" for e in entries)
        expected = f"# {path.stem}\n\n{body}\n" if body else f"# {path.stem}\n"
        assert path.read_text() == expected


def test_a_subject_cluster_proposal_keeps_every_distinct_fact(session):
    a = add(
        session,
        "prompt_builder.render_diff_budget must allocate by the DELIVERED token count: "
        "rendering stops on whole-hunk boundaries, so the token target is not what a "
        "`per_file` share receives.",
    )
    b = add(
        session,
        "prompt_builder._floor_only_reason must compare `max(per_file, cap)`, not `per_file`: "
        "a per-file-only check prints a reason whose numbers contradict the budget.",
    )
    assert is_duplicate_pair(a.content, b.content, build_df([a.content, b.content]))[0] is False
    proposal = next(p for p in m.condense(session, "demo")["proposals"] if b.id in p["ids"])
    assert proposal["distinct_facts"] is True
    assert a.content in proposal["content"]
    assert b.content in proposal["content"]

    m.condense(session, "demo", apply_ids=proposal["ids"])
    session.expire_all()
    survivor = session.get(MemoryEntry, proposal["survivor"])
    assert survivor.approved is True
    assert a.content in survivor.content
    assert b.content in survivor.content
    absorbed = next(i for i in proposal["ids"] if i != proposal["survivor"])
    assert session.get(MemoryEntry, absorbed).rejected is True


def test_condense_requires_a_project_and_never_groups_global_or_role_rows(session, budgets):
    # Global and role rows share generic identifiers (`handoff.json`); the old optional
    # --project grouped them into one proposal. Per-project condense must ignore them.
    add(
        session,
        "Write handoff.json early and keep improving it; handoff.md is the human-readable copy.",
        category="global",
        scope="patterns",
        slug=None,
    )
    add(
        session,
        "When writing a handoff.json brief, do not require the artifact the brief forbids.",
        category="global",
        scope="patterns",
        slug=None,
    )
    with pytest.raises(TypeError):
        m.condense(session)  # type: ignore[call-arg]


# ------------------------------------------------ lifecycle behind a contract change (DoD 4)


def test_lifecycle_fields_stay_behind_a_contract_change_request():
    doc = Path("docs/contract-change-request-T13-memory-lifecycle.md")
    assert doc.is_file()
    text = doc.read_text()
    assert "supersedes" in text
    assert "stale_after" in text


def test_the_frozen_memory_table_has_no_lifecycle_columns_yet():
    assert not hasattr(MemoryEntry, "supersedes")
    assert not hasattr(MemoryEntry, "stale_after")


# ------------------------------------------------- memory redaction is recorded (DoD 6)


KEY_VALUE_PHRASE = "The max_log_tokens: a-short-phrase is a config name, not a secret."


def test_adding_a_key_value_phrase_records_the_substitution(session):
    # Pending on purpose: `create(approved=True)` refuses a row the redactor rewrote, so the
    # recorded-substitution path is exercised on the unapproved shape it is allowed in.
    entry = add(session, KEY_VALUE_PHRASE, approved=False)
    record = [t for t in entry.tags_json if t.startswith(m.REDACTION_TAG_PREFIX)]
    assert len(record) == 1
    assert "assignment:count=1" in record[0]
    assert "lines=1" in record[0]


def test_updating_a_key_value_phrase_records_the_substitution(session):
    entry = add(session, "A clean durable rule about the scheduler.")
    updated = m.update(session, entry.id, MemoryUpdate(content=KEY_VALUE_PHRASE))
    record = [t for t in updated.tags_json if t.startswith(m.REDACTION_TAG_PREFIX)]
    assert len(record) == 1
    assert "assignment" in record[0]


def test_a_marked_entry_is_garbled_not_rendered_and_not_approvable(session):
    entry = add(session, "The stored value is " + REDACTED + " here.", approved=False)
    triage = mt.triage(session, status="pending", project_slug="demo")
    row = next(r for r in triage["rows"] if r["id"] == entry.id)
    assert "redaction-marker" in {f["code"] for f in row["flags"]}
    assert row["garbled"] is True
    assert row["suggested_action"] == "rewrite-durable"
    with pytest.raises(m.MemoryError_) as exc:
        m.approve(session, entry.id)
    assert "redaction marker" in str(exc.value)


def test_a_marked_approved_entry_is_not_injected_or_rendered(session):
    # A legacy row can already be approved and garbled; `create(approved=True)` now refuses
    # that shape (see the test below), so insert it the way old data arrived: directly.
    entry = MemoryEntry(
        category="global",
        scope="environment",
        content="Global rule with a garbled value " + REDACTED + " that must not reach a prompt.",
        approved=True,
        source="legacy",
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    assert entry.content not in m.for_prompt(session, "worker", None)["global"]
    assert entry.content not in (m.memory_root() / "global" / "environment.md").read_text()


def test_creating_an_approved_key_value_entry_is_refused_as_garbled(session):
    """Finding 1: `create(approved=True)` must apply the same garbled refusal as approve()."""
    before = len(m.list_entries(session))
    with pytest.raises(m.MemoryError_) as exc:
        add(session, KEY_VALUE_PHRASE)  # add() defaults to approved=True
    assert "redaction marker" in str(exc.value)
    assert "creating approved entry" in str(exc.value)
    assert len(m.list_entries(session)) == before


def test_importing_an_approved_key_value_entry_is_refused_as_garbled(session):
    payload = {
        "version": 1,
        "entries": [
            {
                "category": "project",
                "scope": "gotchas",
                "project_slug": "demo",
                "content": KEY_VALUE_PHRASE,
                "approved": True,
            }
        ],
    }
    before = len(m.list_entries(session))
    with pytest.raises(m.MemoryError_) as exc:
        m.import_all(session, payload)
    assert "redaction marker" in str(exc.value)
    assert "importing approved entry" in str(exc.value)
    assert len(m.list_entries(session)) == before


def test_log_redaction_is_still_a_security_control():
    assert REDACTED in redact("API_TOKEN=abcdef123456")
    assert redact("max_diff_tokens=12000") == "max_diff_tokens=12000"


# ------------------------------------------- approval eviction is refused (DoD 7)


def test_creating_an_already_approved_row_is_refused_when_it_would_evict(session, budgets):
    keep = add(session, "Keep rule " + "alpha " * 10)
    at_risk = add(session, "At-risk rule " + "beta " * 10)
    budgets(max_memory_tokens_project=tokens(keep) + tokens(at_risk))
    before = len(m.list_entries(session))

    with pytest.raises(m.MemoryError_) as exc:
        add(session, "New rule " + "gamma " * 10)
    assert "creating" in str(exc.value)
    # Oldest-first among equal-class untagged rows: the OLDER approved row is the casualty.
    assert f"#{keep.id} (" in str(exc.value)
    assert len(m.list_entries(session)) == before
    # The explicit opt-in is the only way through; the evicted row stays approved, not deleted.
    created = m.create(
        session,
        MemoryCreate(
            category="project",
            scope="gotchas",
            project_slug="demo",
            content="New rule " + "gamma " * 10,
            approved=True,
        ),
        override=True,
    )
    assert created.approved is True
    session.expire_all()
    assert session.get(MemoryEntry, keep.id).approved is True
    assert keep.content not in m.for_prompt(session, "worker", "demo")["project"]
    assert at_risk.content in m.for_prompt(session, "worker", "demo")["project"]


def test_importing_an_approved_row_is_refused_when_it_would_evict(session, budgets):
    keep = add(session, "Keep rule " + "alpha " * 10)
    at_risk = add(session, "At-risk rule " + "beta " * 10)
    budgets(max_memory_tokens_project=tokens(keep) + tokens(at_risk))
    payload = {
        "version": 1,
        "entries": [
            {
                "category": "project",
                "scope": "gotchas",
                "project_slug": "demo",
                "content": "Imported rule " + "gamma " * 10,
                "approved": True,
            }
        ],
    }
    before = len(m.list_entries(session))
    with pytest.raises(m.MemoryError_) as exc:
        m.import_all(session, payload)
    assert "importing approved entry" in str(exc.value)
    assert f"#{keep.id} (" in str(exc.value)
    assert len(m.list_entries(session)) == before

    assert m.import_all(session, payload, override=True) == 1


def test_triage_flags_a_linked_restatement_as_a_merge_candidate(session):
    add(session, ANCHOR_RULE)
    out = m.suggest_from_handoff_report(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=ANCHOR_RESTATED)],
        source="handoff:R-16",
        project_slug="demo",
    )
    row = next(
        r for r in mt.triage(session, status="pending", project_slug="demo")["rows"]
        if r["id"] == out.linked[0]["entry_id"]
    )
    assert row["duplicate_of"] == out.linked[0]["existing_id"]
    assert "linked-duplicate" in {f["code"] for f in row["flags"]}
    assert row["suggested_action"] == "merge"


def test_approval_that_would_evict_names_the_casualty_and_needs_override(session, budgets):
    keep = add(session, "Keep rule " + "alpha " * 10)
    at_risk = add(session, "At-risk rule " + "beta " * 10)
    pending = add(session, "New rule " + "gamma " * 10, approved=False)
    budgets(max_memory_tokens_project=tokens(keep) + tokens(at_risk))
    casualty = m.approval_impact(session, pending.id)["evicted"][0]["id"]

    with pytest.raises(m.MemoryError_) as exc:
        m.approve(session, pending.id)
    assert f"#{casualty} (" in str(exc.value)
    session.expire_all()
    assert session.get(MemoryEntry, pending.id).approved is False

    m.approve(session, pending.id, override=True)
    session.expire_all()
    assert session.get(MemoryEntry, pending.id).approved is True
    assert session.get(MemoryEntry, casualty).content not in m.for_prompt(
        session, "worker", "demo"
    )["project"]


def test_approving_a_row_that_would_itself_be_demoted_is_refused(session, budgets):
    """Finding 2: an approval that will not inject the row must not report plain success."""
    keep = add(session, "Keep rule " + "alpha " * 10)
    pending = add(session, "T-99 note: " + "gamma " * 10, approved=False)
    budgets(max_memory_tokens_project=tokens(keep))

    impact = m.approval_impact(session, pending.id)
    assert [row["id"] for row in impact["evicted"]] == [pending.id]
    assert impact["evicted"][0]["self"] is True

    with pytest.raises(m.MemoryError_) as exc:
        m.approve(session, pending.id)
    assert f"#{pending.id} (" in str(exc.value)
    assert "this row itself would not be injected" in str(exc.value)
    session.expire_all()
    assert session.get(MemoryEntry, pending.id).approved is False

    m.approve(session, pending.id, override=True)
    session.expire_all()
    assert session.get(MemoryEntry, pending.id).approved is True
    assert pending.content not in m.for_prompt(session, "worker", "demo")["project"]


def test_override_records_the_demoted_ids_in_the_build_log(session, budgets):
    from sqlmodel import select

    from orchestrator.core.models import BuildLogEntry

    keep = add(session, "Keep rule " + "alpha " * 10)
    pending = add(session, "T-99 note: " + "gamma " * 10, approved=False)
    budgets(max_memory_tokens_project=tokens(keep))
    casualty = m.approval_impact(session, pending.id)["evicted"][0]["id"]

    m.approve(session, pending.id, override=True)
    rows = list(
        session.exec(
            select(BuildLogEntry).where(BuildLogEntry.event == "memory_eviction_override")
        )
    )
    assert len(rows) == 1
    assert f"#{casualty}" in rows[0].details


def test_budget_and_triage_name_the_current_demotions(session, budgets):
    keep = add(session, "Keep rule " + "alpha " * 10)
    at_risk = add(session, "At-risk rule " + "beta " * 10)
    pending = add(session, "New rule " + "gamma " * 10, approved=False)
    budgets(max_memory_tokens_project=tokens(keep) + tokens(at_risk))
    casualty = m.approval_impact(session, pending.id)["evicted"][0]["id"]
    m.approve(session, pending.id, override=True)

    report = mt.budget_report(session, "demo")
    assert casualty in report["roles"]["worker"]["excluded_now"]
    triage = mt.triage(session, status="approved", project_slug="demo")
    assert casualty in [x["id"] for x in triage["excluded_now"]]
