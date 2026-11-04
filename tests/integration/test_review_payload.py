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


def test_refuted_absence_finding_is_recorded_and_does_not_requeue(session, project):
    task, outcome = _run(session, project, ["mock", "mock-refute"])
    # The mock reviewer said README.md was missing; it is present in this worktree. The finding
    # must be refuted, and a CHANGES_REQUESTED verdict that rested only on it must not spend a
    # Worker round.
    assert outcome["verdict"] == "APPROVED", outcome
    assert task.status == "accepted"

    review = session.exec(select(Review).order_by(col(Review.id).desc())).first()
    assert review is not None and review.verdict == "APPROVED"
    assert review.findings == []
    notes = review.reasoning or ""
    assert "absence check [refuted] README.md" in notes
    assert "refuted absence finding removed before it could cost a round" in notes

    payload = _payload(outcome)
    assert "| README.md | finding | **refuted** |" in payload
    assert "file exists in the task worktree" in payload

    events = _events(session, task, "review_absence_refuted")
    assert len(events) == 1
    assert "README.md" in events[0].details


def test_harness_and_reviewer_missing_artifacts_live_in_separate_payload_sections(
    session, project
):
    task, outcome = _run(session, project, ["mock", "mock-assert-missing-artifact"])
    payload = _payload(outcome)
    harness_at = payload.index("## Harness-detected missing artifacts")
    reviewer_at = payload.index("## Reviewer-asserted missing artifacts")
    checks_at = payload.index("## Absence checks against the task worktree")
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
