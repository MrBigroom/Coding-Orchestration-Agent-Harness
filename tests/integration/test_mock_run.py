"""End-to-end role loop with the mock harness + mock provider against a real git repo."""

import asyncio

import pytest

from orchestrator.adapters.mock_harness import MockHarnessAdapter
from orchestrator.core.models import Review, Verification
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
    memory_service,
    project_service,
    run_service,
    settings_service,
    task_service,
)
from orchestrator.utils.git import branch_exists, current_branch
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

RUN_TESTS = "import sys; print('2 passed, 0 failed'); sys.exit(0)\n"


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(tmp_path / "proj", {"run_tests.py": RUN_TESTS, "README.md": "x\n"})
    return project_service.create(
        session, ProjectCreate(name="proj", path=str(repo), test_command="python3 run_tests.py")
    )


def _run(session, project, tags, phase=1, **kw):
    task = task_service.create(
        session,
        TaskCreate(
            title="mock phase",
            project_id=project.id,
            tags=tags,
            phase_number=phase,
            phase_slug=f"p{phase}",
            definition_of_done=["MOCK_CHANGE.md updated"],
            **kw,
        ),
    )
    outcome = asyncio.run(run_service.run_task_now(session, task))
    session.refresh(task)
    return task, outcome


def test_happy_path_accepts_with_all_artifacts(session, project):
    task, outcome = _run(session, project, ["mock"])
    assert task.status == "accepted", outcome
    assert task.review_status == "APPROVED" and task.verification_status == "pass"
    assert task.merge_status == "ready" and "merge --no-ff" in outcome["merge_command"]
    names = {a["name"] for a in artifact_service.list_artifacts(outcome["run_id"])}
    assert {
        "handoff.json",
        "handoff.md",
        "review.json",
        "review.md",
        "verification.json",
        "verification.md",
        "patch.diff",
        "stdout.log",
        "test.log",
        "prompt.md",
    } <= names
    assert "MOCK_CHANGE.md" in artifact_service.read_text(outcome["run_id"], "patch.diff")
    from sqlmodel import select

    assert len(session.exec(select(Review)).all()) == 1
    assert len(session.exec(select(Verification)).all()) == 1
    # main is untouched; the phase branch is the merge candidate
    from pathlib import Path

    assert current_branch(Path(project.path)) == "main"
    assert not (Path(project.path) / "MOCK_CHANGE.md").exists()
    assert branch_exists(Path(project.path), "phase-1-p1")
    # memory suggestions are stored unapproved
    pending = memory_service.list_entries(session, status="pending")
    assert len(pending) == 2


def test_blocked_handoff_blocks_task(session, project):
    task, outcome = _run(session, project, ["mock", "mock-blocked"])
    assert task.status == "blocked" and outcome["decision"] == "blocked"
    assert task.handoff_status == "BLOCKED"


def test_failure_and_timeout_return_task_to_planned(session, project):
    task, outcome = _run(session, project, ["mock-fail"], phase=2)
    assert outcome["status"] == "failed" and task.status == "planned"
    task2, outcome2 = _run(session, project, ["mock-timeout"], phase=3)
    assert outcome2["status"] == "timeout" and task2.status == "planned"


def test_adapter_error_with_exit_zero_fails_the_run(session, project, monkeypatch):
    original = MockHarnessAdapter.run

    async def cut_off(self, ctx, runner):
        result = await original(self, ctx, runner)
        result.error = "Pi stopped early (stopReason: length) without a handoff"
        return result

    monkeypatch.setattr(MockHarnessAdapter, "run", cut_off)
    task, outcome = _run(session, project, ["mock"], phase=2)
    assert outcome["status"] == "failed" and task.status == "planned"
    assert "stopReason: length" in outcome["error"]


def test_changes_requested_requeues_then_blocks_after_round_limit(session, project):
    settings_service.update(session, {"max_review_rounds": 2})
    task, outcome = _run(session, project, ["mock", "mock-reject"])
    assert outcome["verdict"] == "CHANGES_REQUESTED" and task.status == "queued"
    outcome = asyncio.run(run_service.run_task_now(session, task))
    session.refresh(task)
    assert task.status == "blocked"  # second round hits the limit -> needs a human


def test_ambiguous_review_fails_closed(session, project):
    task, outcome = _run(session, project, ["mock", "mock-ambiguous"])
    assert outcome["verdict"] == "CHANGES_REQUESTED"
    assert artifact_service.read_text(outcome["run_id"], "review.raw.txt")


def test_failing_project_tests_skip_review(session, tmp_path):
    repo = make_repo(
        tmp_path / "red", {"run_tests.py": "import sys; print('0 passed, 1 failed'); sys.exit(1)\n"}
    )
    proj = project_service.create(
        session, ProjectCreate(name="red", path=str(repo), test_command="python3 run_tests.py")
    )
    task, outcome = _run(session, proj, ["mock"])
    assert outcome["verification"] == "fail" and "verdict" not in outcome
    assert task.status == "queued" and task.review_status is None


def test_merge_gate_enabled_merges_no_ff(session, project):
    settings_service.update(session, {"merge_enabled": True, "auto_merge_enabled": True})
    task, outcome = _run(session, project, ["mock"])
    assert outcome.get("merged") is True and task.merge_status == "merged"
    from pathlib import Path

    assert (Path(project.path) / "MOCK_CHANGE.md").exists()


def test_cancel_marks_run_cancelled(session, project):
    import threading
    import time

    from orchestrator.core.models import Run

    task = task_service.create(
        session,
        TaskCreate(
            title="slow",
            project_id=project.id,
            tags=["mock-timeout"],
            phase_number=8,
            phase_slug="slow",
        ),
    )

    def cancel_soon():
        time.sleep(0.6)
        from orchestrator.core.db import session_scope

        with session_scope() as s:
            run_service.cancel(s, task_service.get(s, task.id))

    threading.Thread(target=cancel_soon).start()
    outcome = asyncio.run(run_service.run_task_now(session, task))
    assert outcome["status"] == "cancelled"
    assert session.get(Run, outcome["run_id"]).status == "cancelled"


def test_demo_command_runs_offline(session):
    from orchestrator.services import demo_service

    result = asyncio.run(demo_service.run_demo(session))
    assert result["status"] == "accepted" and result["approved_memory_id"]
