"""T-15 end to end: the Supervisor verification pass reports stale committed evidence.

Offline: local git repo, the project's own trivial test command, no harness and no network. The
row is additive to verification.json — the frozen check-record keys, the existing check names and
the frozen artifact names are all asserted unchanged here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlmodel import select

from orchestrator.core import config as config_mod
from orchestrator.core.models import BuildLogEntry, Run
from orchestrator.core.schemas import Handoff, HandoffTests, ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
    evidence_freshness,
    project_service,
    supervisor_service,
    task_service,
)
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
FROZEN_KEYS = {"name", "status", "command", "evidence", "notes"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV
    ).stdout


def _commit(repo: Path, subject: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(
        tmp_path / "proj",
        {
            "run_tests.py": "print('3 passed, 0 failed')\n",
            "README.md": "x\n",
            "docs/frozen-contracts.md": "# frozen contracts\n",
        },
    )
    project = project_service.create(
        session,
        ProjectCreate(name="proj", path=str(repo), test_command="python3 run_tests.py"),
    )
    # Work happens on a task branch, as in the harness: the deletion scan is `main..commit`.
    _git(repo, "checkout", "-q", "-b", "task")
    return project


def _task(session, project, phase: int = 1):
    return task_service.create(
        session,
        TaskCreate(
            title=f"T-15 freshness probe {phase}",
            project_id=project.id,
            tags=["mock"],
            phase_number=phase,
            phase_slug=f"p{phase}",
            definition_of_done=["evidence matches the commit under review"],
        ),
    )


def _verify(session, task, project, commit_sha: str):
    run = Run(
        short_id=f"R-T15-{task.id}",
        task_id=task.id,
        status="testing",
        worktree_path=project.path,
        branch_name="task",
        commit_sha=commit_sha,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    handoff = Handoff(
        phase="phase-1",
        branch="main",
        status="READY_FOR_REVIEW",
        summary="freshness probe",
        tests=HandoffTests(command="python3 run_tests.py", passed=3),
    )
    result = asyncio.run(supervisor_service.verify(session, task, run, project, handoff, False))
    return run, {check.name: check for check in result.checks}, result


def _stamped(label: str, sha: str) -> dict[str, str]:
    return {f"verify-evidence/{label}/full_suite.txt": f"# HEAD={sha}\n3 passed\n"}


def _stale_evidence(session, project, task, tmp_path):
    """A bundle stamped at an older commit, then one code commit on top of it."""
    repo = Path(project.path)
    old = _commit(repo, "evidence", {f"verify-evidence/{task.short_id}/old.txt": "old\n"})
    _commit(repo, "evidence stamped at the round-4 tip", _stamped(task.short_id, old))
    return _commit(repo, "round 5 code", {"src/app.py": "x = 2\n"})


def test_stale_evidence_is_reported_without_flipping_the_verdict(session, project, tmp_path):
    task = _task(session, project)
    new = _stale_evidence(session, project, task, tmp_path)
    run, checks, result = _verify(session, task, project, new)

    fresh = checks[evidence_freshness.CHECK_NAME]
    # CCR-T15-01 (approved 2026-11-08): `stale` is the fifth status value, so a machine consumer
    # never has to parse prose. The reason names the count, the paths and the commit it compared.
    assert fresh.status == "stale"
    assert fresh.evidence == (
        f"1 stale of 1 stamped evidence file(s) against "
        f"{evidence_freshness._short(Path(project.path), new)} ({new})"
    )
    assert f"verify-evidence/{task.short_id}/full_suite.txt" in fresh.notes
    assert result.final_result == "pass"  # reported, never a failing check by default
    assert result.contradicts_worker is False
    # The machine-readable state is in the build-log payload, next to the human string.
    events = [e for e in session.exec(select(BuildLogEntry)) if e.event == "evidence_freshness_stale"]
    payload = json.loads(events[-1].details.split("findings_json=", 1)[1])
    assert payload["state"] == "stale"
    assert payload["check_status"] == "stale"


def test_the_existing_checks_and_frozen_artifact_names_are_unchanged(session, project):
    task = _task(session, project, phase=2)
    first = _commit(Path(project.path), "code", {"src/app.py": "x = 1\n"})
    head = _commit(Path(project.path), "evidence at the code HEAD", _stamped(task.short_id, first))
    run, checks, result = _verify(session, task, project, head)

    assert {"tests", "migrations apply/downgrade", "endpoints against live stack"} <= set(checks)
    names = {a["name"] for a in artifact_service.list_artifacts(run.id)}
    assert {"test.log", "test_summary.txt", "verification.json", "verification.md"} <= names
    saved = json.loads(artifact_service.read_text(run.id, "verification.json"))
    assert {c["name"] for c in saved["checks"]} == set(checks)
    assert all(set(c) == FROZEN_KEYS for c in saved["checks"])
    assert checks[evidence_freshness.CHECK_NAME].status == "pass"
    assert result.final_result == "pass"


def test_an_absent_bundle_is_not_applicable(session, project):
    task = _task(session, project, phase=3)
    head = _commit(Path(project.path), "code without evidence", {"src/app.py": "x = 1\n"})
    _run, checks, result = _verify(session, task, project, head)
    assert checks[evidence_freshness.CHECK_NAME].status == "not_applicable"
    assert result.final_result == "pass"


def test_strict_mode_is_the_only_way_staleness_fails_the_gate(session, project, monkeypatch):
    task = _task(session, project, phase=4)
    new = _stale_evidence(session, project, task, None)
    monkeypatch.setenv("ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT", "true")
    config_mod.reset_settings_cache()
    try:
        _run, checks, result = _verify(session, task, project, new)
    finally:
        monkeypatch.delenv("ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT", raising=False)
        config_mod.reset_settings_cache()
    assert checks[evidence_freshness.CHECK_NAME].status == "fail"
    assert result.final_result == "fail"


def test_deleted_evidence_is_reported_with_the_deleting_commit(session, project):
    """Deleting evidence to fit a review budget must not read as `not_applicable`.

    Distinct from a task that never committed evidence: this bundle existed on the branch, and the
    row must name both the path and the commit that removed it.
    """
    task = _task(session, project, phase=5)
    repo = Path(project.path)
    first = _commit(
        repo,
        "evidence",
        {
            f"verify-evidence/{task.short_id}/README.md": "# evidence\n",
            f"verify-evidence/{task.short_id}/full_suite.txt": "# HEAD=PARENT\n3 passed\n",
        },
    )
    _commit(repo, "evidence re-stamped", _stamped(task.short_id, first))
    (repo / f"verify-evidence/{task.short_id}/README.md").unlink()
    deleted = _commit(repo, "test(phase-5): delete README evidence", {})

    _run, checks, result = _verify(session, task, project, deleted)
    fresh = checks[evidence_freshness.CHECK_NAME]
    assert fresh.status == "stale"
    assert f"verify-evidence/{task.short_id}/README.md: deleted by {deleted}" in fresh.notes
    assert "1 deleted evidence file(s)" in fresh.evidence
    assert result.contradicts_worker is False


def test_never_committed_evidence_is_not_applicable_not_a_deletion(session, project):
    task = _task(session, project, phase=6)
    head = _commit(Path(project.path), "code with no evidence ever", {"src/app.py": "x = 1\n"})
    _run, checks, _result = _verify(session, task, project, head)
    fresh = checks[evidence_freshness.CHECK_NAME]
    assert fresh.status == "not_applicable"
    assert "deleted" not in fresh.evidence
    assert fresh.notes == ""


def test_an_agent_kind_task_gets_neither_the_row_nor_the_finding(session, project):
    """One scope for both call sites (T-15): an agent task writes no code and has no bundle.

    The control matters: the agent's OWN evidence directory is stale at the same commit and the raw
    check reads `stale` for it, so this test fails if the scope guard is removed from either call
    site rather than passing because there was nothing to compare.
    """
    from orchestrator.core.models import TaskKind
    from orchestrator.services import review_service

    coding = _task(session, project, phase=7)
    agent = task_service.create(
        session,
        TaskCreate(
            title="T-15 freshness probe (agent)",
            project_id=project.id,
            kind=TaskKind.agent,
            tags=["mock"],
            phase_number=8,
            phase_slug="p8",
            definition_of_done=["answer the question"],
        ),
    )
    repo = Path(project.path)
    old = _commit(
        repo,
        "evidence",
        {
            f"verify-evidence/{coding.short_id}/full_suite.txt": "# HEAD=PARENT\n3 passed\n",
            f"verify-evidence/{agent.short_id}/full_suite.txt": "# HEAD=PARENT\n3 passed\n",
        },
    )
    _commit(
        repo,
        "evidence stamped at the round-4 tip",
        {**_stamped(coding.short_id, old), **_stamped(agent.short_id, old)},
    )
    new = _commit(repo, "round 5 code", {"src/app.py": "x = 2\n"})

    # The control: the agent's own bundle really is stale if anything compared it.
    assert (
        evidence_freshness.check_freshness(repo, new, agent.short_id, project.base_branch).state
        == evidence_freshness.STATE_STALE
    )

    coding_run, coding_checks, _result = _verify(session, coding, project, new)
    assert coding_checks[evidence_freshness.CHECK_NAME].status == "stale"
    assert (
        review_service.review_freshness(
            coding, repo, coding_run, project.base_branch
        ).state
        == evidence_freshness.STATE_STALE
    )

    agent_run, agent_checks, _result = _verify(session, agent, project, new)
    assert evidence_freshness.CHECK_NAME not in agent_checks
    agent_review = review_service.review_freshness(agent, repo, agent_run, project.base_branch)
    assert agent_review.state == evidence_freshness.STATE_NOT_APPLICABLE
    assert agent_review.stale_paths == []
