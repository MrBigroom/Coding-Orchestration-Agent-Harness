"""T-11: archived reviewer input, provenance for absence claims, no round lost to a void finding."""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import col, select

from orchestrator.core.config import get_settings
from orchestrator.core.models import BuildLogEntry, Review
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import artifact_service, project_service, run_service, task_service
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

RUN_TESTS = "import sys; print('2 passed, 0 failed'); sys.exit(0)\n"


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(tmp_path / "proj", {"run_tests.py": RUN_TESTS, "README.md": "x\n"})
    return project_service.create(
        session, ProjectCreate(name="proj", path=str(repo), test_command="python3 run_tests.py")
    )


def _run(session, project, tags, phase=1):
    task = task_service.create(
        session,
        TaskCreate(
            title="review payload",
            project_id=project.id,
            tags=tags,
            phase_number=phase,
            phase_slug=f"p{phase}",
            definition_of_done=["MOCK_CHANGE.md updated"],
        ),
    )
    outcome = asyncio.run(run_service.run_task_now(session, task))
    session.refresh(task)
    return task, outcome


def _payload(outcome) -> str:
    text = artifact_service.read_text(outcome["run_id"], "review_payload.md")
    assert text is not None
    return text


def _events(session, task, event: str) -> list[BuildLogEntry]:
    return list(
        session.exec(
            select(BuildLogEntry)
            .where(BuildLogEntry.task_id == task.id)
            .where(BuildLogEntry.event == event)
            .order_by(col(BuildLogEntry.id))
        )
    )


def test_review_payload_is_archived_and_names_what_truncation_dropped(
    session, project, monkeypatch
):
    monkeypatch.setattr(get_settings(), "max_log_tokens", 5)
    task, outcome = _run(session, project, ["mock"])
    assert task.status == "accepted", outcome
    names = {a["name"] for a in artifact_service.list_artifacts(outcome["run_id"])}
    assert "review_payload.md" in names

    payload = _payload(outcome)
    assert "## Exact reviewer input (static_text() + dynamic_text())" in payload
    assert "## Truncation manifest" in payload
    # max_log_tokens=5 forces the handoff block through truncate_middle, and the manifest must
    # name it with the bytes it dropped.
    assert "handoff block" in payload
    assert "max_log_tokens=5 tokens" in payload
    assert "bytes available" in payload and "bytes included" in payload
    assert "You are the Architect/Reviewer" in payload


def test_refuted_absence_finding_does_not_requeue_and_never_becomes_an_approval(
    session, project
):
    task, outcome = _run(session, project, ["mock", "mock-refute"])
    # The mock reviewer said README.md was missing; it is present in this worktree. The finding
    # must be refuted, the round must not go back to the Worker, and the harness must NOT upgrade
    # the reviewer's CHANGES_REQUESTED to APPROVED: refuting an absence claim proves the reviewer
    # was wrong about the evidence, not that the code is acceptable (round-2 reviewer finding 1).
    assert outcome["verdict"] == "NEEDS_HUMAN", outcome
    assert task.status == "blocked"
    assert task.review_status == "NEEDS_HUMAN"
    # blocked is reached without a Worker retry: the task was never put back in the queue.
    assert task.source != "retry"

    review = session.exec(select(Review).order_by(col(Review.id).desc())).first()
    assert review is not None and review.verdict == "NEEDS_HUMAN"
    notes = review.reasoning or ""
    assert "absence check [refuted] README.md" in notes
    assert "refuted absence finding removed before it could cost a round" in notes
    assert "verdict held at NEEDS_HUMAN (never upgraded)" in notes
    # The refuted claim is gone; what remains is the harness's own routing finding.
    assert [f["file_or_area"] for f in review.findings] == ["review verdict"]

    payload = _payload(outcome)
    assert "| README.md | finding | **refuted** |" in payload
    assert "file is tracked at the reviewed commit" in payload

    events = _events(session, task, "review_absence_refuted")
    assert len(events) == 1
    assert "README.md" in events[0].details
    # A refuted claim is never a reason to accept.
    assert not _events(session, task, "task_accepted")


def test_visibility_claim_is_refuted_by_the_supplied_patch_not_by_the_file_existing(
    session, project
):
    """T-11 round-2 finding 2: existence must not refute "was not supplied"."""
    task, outcome = _run(session, project, ["mock", "mock-visibility"])
    payload = _payload(outcome)
    # MOCK_CHANGE.md exists in the worktree, but it was also supplied in full — that, and only
    # that, is what refutes the reviewer's claim.
    assert "| MOCK_CHANGE.md | finding | **refuted** |" in payload
    assert "supplied to the reviewer in full" in payload
    assert "file exists in the task worktree" not in payload
    review_json = artifact_service.read_json(outcome["run_id"], "review.json") or {}
    assert review_json["verdict"] == "NEEDS_HUMAN"
    # DoD 2: review.json states which findings the harness could settle mechanically.
    assert any(
        line.startswith("mechanical absence checks: 0 confirmed, 1 refuted, 0 unchecked")
        for line in review_json.get("notes", [])
    )


def test_harness_and_reviewer_missing_artifacts_live_in_separate_payload_sections(
    session, project
):
    task, outcome = _run(session, project, ["mock", "mock-assert-missing-artifact"])
    payload = _payload(outcome)
    harness_at = payload.index("## Harness-detected missing artifacts")
    reviewer_at = payload.index("## Reviewer-asserted missing artifacts")
    checks_at = payload.index("## Absence checks (existence claims vs visibility claims)")
    assert harness_at < reviewer_at < checks_at
    assert "docs/not-in-repo.md" in payload[reviewer_at:checks_at]
    # The harness section is not where the model's claim appears.
    assert "docs/not-in-repo.md" not in payload[harness_at:reviewer_at]

    review = session.exec(select(Review).order_by(col(Review.id).desc())).first()
    assert review is not None
    missing = [m for m in f"{review.reasoning or ''}".splitlines()]
    assert any("docs/not-in-repo.md" in line for line in missing)
    review_json = artifact_service.read_json(outcome["run_id"], "review.json") or {}
    assert any(
        m.startswith("reviewer-asserted:") and "docs/not-in-repo.md" in m
        for m in review_json.get("missing_artifacts", [])
    )


def test_manifest_names_the_files_a_diff_budget_dropped(session, project, monkeypatch):
    """DoD 5(d): a forced cut names the dropped files, in the archive and in the prompt."""
    monkeypatch.setattr(get_settings(), "max_diff_tokens", 1)
    task, outcome = _run(session, project, ["mock"])
    payload = _payload(outcome)
    manifest_at = payload.index("## Truncation manifest")
    supplied_at = payload.index("## What the reviewer was actually given")
    manifest = payload[manifest_at:supplied_at]
    assert "diff:MOCK_CHANGE.md" in manifest
    assert "omitted because the diff budget was exhausted" in manifest
    # The exact input shows the omission instead of an empty diff.
    assert "omitted (1): MOCK_CHANGE.md" in payload
    assert "files the truncation manifest records as cut: MOCK_CHANGE.md (0/" in payload


def test_archived_payload_carries_the_resolution_evidence_for_each_verdict(session, project):
    """T-22 DoD 4: review_payload.md states which path was tried, where it was found, and which
    sources were searched — a verdict without that provenance is not auditable."""
    task, outcome = _run(session, project, ["mock", "mock-assert-missing-artifact"])
    payload = _payload(outcome)
    # The reviewer asserted `docs/not-in-repo.md`: an explicit path, absent from every source, so
    # it is the one state that deserves the strongest label — with its resolution shown next to it.
    assert "| docs/not-in-repo.md | missing_artifact | **confirmed** |" in payload
    assert "absent-at-reviewed-commit" in payload
    assert "explicit-named-path (tried: docs/not-in-repo.md)" in payload
    assert "listed by none of these sources" in payload
    # The universe the whole round was resolved against is stated once, with the commit it covers.
    assert "path claims were resolved against:" in payload
    assert "git ls-tree HEAD" in payload
    assert "a bare name matching exactly one path is resolved to it" in payload
    # And the tally in review.json says no confirmation came from a lookup that found nothing.
    review_json = artifact_service.read_json(outcome["run_id"], "review.json") or {}
    tally = next(
        n for n in review_json["notes"] if n.startswith("mechanical absence checks:")
    )
    assert "from a failed lookup: 0" in tally


def test_a_file_the_budget_withheld_is_archived_as_the_budget_state_not_as_absent(
    session, project, monkeypatch
):
    """T-22 DoD 3/5(d) end to end: present in the worktree, omitted by the diff budget.

    This is run 38's confusion. The reviewer's claim is true about its own input and false about
    the repository, and the archive has to say which of the two it is — the second is a Worker
    evidence gap, the first is the diff budget's (T-16).
    """
    monkeypatch.setattr(get_settings(), "max_diff_tokens", 1)
    task, outcome = _run(session, project, ["mock", "mock-visibility"])
    payload = _payload(outcome)
    # DoD 2: the path is located but the repository contains it, so the strongest label is not
    # available; the state carries the budget meaning instead of a false confirmed absence.
    assert "| MOCK_CHANGE.md | finding | **unchecked** |" in payload
    assert "omitted-from-review-input" in payload
    assert "this is not a Worker evidence gap" in payload
    # The archived payload's diff shows the file was omitted by the budget, which is the cause.
    assert "omitted (1): MOCK_CHANGE.md" in payload
    review_json = artifact_service.read_json(outcome["run_id"], "review.json") or {}
    tally = next(
        n for n in review_json["notes"] if n.startswith("mechanical absence checks:")
    )
    assert "omitted-from-review-input" in tally
    assert "absent-at-reviewed-commit" not in tally
