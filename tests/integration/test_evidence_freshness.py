"""T-15 end to end: the Supervisor verification pass reports stale committed evidence.

Offline: local git repo, the project's own trivial test command, no harness or network. The check
is additive to verification.json; the existing check names and artifact names stay untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.core import config as config_mod
from orchestrator.core.models import Run
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
RUN_TESTS = "print('3 passed, 0 failed')\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=ENV,
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
            "run_tests.py": RUN_TESTS,
            "README.md": "x\n",
            "docs/frozen-contracts.md": "# frozen contracts\n",
        },
    )
    project = project_service.create(
        session,
        ProjectCreate(
            name="proj",
            path=str(repo),
            test_command="python3 run_tests.py",
        ),
    )
    # Keep the initial commit on `main` and do the Work on a task branch, as the real harness
    # does: the deletion scan is `main..commit`, which is empty on main itself.
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
    result = asyncio.run(
        supervisor_service.verify(session, task, run, project, handoff, False)
    )
    return run, {check.name: check for check in result.checks}, result


def _stamp_file(label: str, stamp: str) -> dict[str, str]:
    return {f"verify-evidence/{label}/full_suite.txt": f"{stamp}\n3 passed\n"}


def test_stale_evidence_is_reported_as_stale_and_does_not_flip_the_verdict(
    session, project, tmp_path
):
    repo = Path(project.path)
    task = _task(session, project)
    label = task.short_id
    old = _commit(repo, "evidence", {f"verify-evidence/{label}/old.txt": "old\n"})
    _commit(
        repo,
        "evidence stamped at the round-4 tip",
        _stamp_file(label, f"# HEAD={old}"),
    )
    new = _commit(repo, "round 5 code", {"src/app.py": "x = 2\n"})
    run, checks, result = _verify(session, task, project, new)

    freshness = checks[evidence_freshness.CHECK_NAME]
    assert freshness.status == "stale"
    assert "1 stale of 1 stamped evidence file(s)" in freshness.evidence
    assert f"verify-evidence/{label}/full_suite.txt" in freshness.notes
    assert result.final_result == "pass"  # reported, never a failing check by default
    assert result.contradicts_worker is False

    # The existing checks and frozen artifact names are untouched; this is an additive row.
    assert {"tests", "migrations apply/downgrade", "endpoints against live stack"} <= set(checks)
    names = {a["name"] for a in artifact_service.list_artifacts(run.id)}
    assert {"test.log", "test_summary.txt", "verification.json", "verification.md"} <= names
    saved = json.loads(artifact_service.read_text(run.id, "verification.json"))
    assert {c["name"] for c in saved["checks"]} == set(checks)
    assert set(saved["checks"][0]) == {"name", "status", "command", "evidence", "notes"}


def test_a_fresh_bundle_is_reported_as_pass(session, project):
    repo = Path(project.path)
    task = _task(session, project, phase=2)
    first = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    head = _commit(
        repo,
        "evidence stamped at the code commit",
        _stamp_file(task.short_id, f"# HEAD={first}"),
    )
    _run, checks, result = _verify(session, task, project, head)
    assert checks[evidence_freshness.CHECK_NAME].status == "pass"
    assert result.final_result == "pass"


def test_an_absent_bundle_is_not_applicable(session, project):
    repo = Path(project.path)
    head = _commit(repo, "code without evidence", {"src/app.py": "x = 1\n"})
    task = _task(session, project, phase=3)
    _run, checks, result = _verify(session, task, project, head)
    assert checks[evidence_freshness.CHECK_NAME].status == "not_applicable"
    assert result.final_result == "pass"


def test_strict_mode_is_the_only_way_staleness_fails_the_gate(
    session, project, monkeypatch
):
    repo = Path(project.path)
    task = _task(session, project, phase=4)
    label = task.short_id
    old = _commit(repo, "evidence", {f"verify-evidence/{label}/old.txt": "old\n"})
    _commit(repo, "evidence stamped", _stamp_file(label, f"# HEAD={old}"))
    new = _commit(repo, "code", {"src/app.py": "x = 3\n"})
    monkeypatch.setenv("ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT", "true")
    config_mod.reset_settings_cache()
    try:
        _run, checks, result = _verify(session, task, project, new)
    finally:
        monkeypatch.delenv("ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT", raising=False)
        config_mod.reset_settings_cache()
    assert checks[evidence_freshness.CHECK_NAME].status == "fail"
    assert result.final_result == "fail"
