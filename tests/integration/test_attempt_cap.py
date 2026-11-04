"""T-12: a review-round cap that counts only completed runs cannot bound paid attempts.

The two counters are deliberately different:
  * `max_review_rounds` — runs that reached the gate (completed, or `testing` while the gate runs);
  * `max_run_attempts_per_task` — every adapter attempt, including timeout/failed/cancelled.

These tests use the mock harness against a real local git repo. Offline: no network, no service.
"""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters import mock_harness
from orchestrator.core.models import Run
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
    log_service,
    project_service,
    run_service,
    settings_service,
    supervisor_service,
    task_service,
)
from orchestrator.utils import structured_log
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

RUN_TESTS = "import sys; print('2 passed, 0 failed'); sys.exit(0)\n"


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(tmp_path / "proj", {"run_tests.py": RUN_TESTS, "README.md": "x\n"})
    return project_service.create(
        session, ProjectCreate(name="proj", path=str(repo), test_command="python3 run_tests.py")
    )


@pytest.fixture
def client(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c


def _task(session, project, tags, phase=1, kind="coding"):
    return task_service.create(
        session,
        TaskCreate(
            title="attempt cap phase",
            project_id=project.id,
            tags=list(tags),
            kind=kind,
            phase_number=phase,
            phase_slug=f"p{phase}",
            definition_of_done=["MOCK_CHANGE.md updated"],
        ),
    )


def _run(session, task, tags=None):
    if tags is not None:
        task = task_service.get(session, task.id)
        task.tags = list(tags)
        session.add(task)
        session.commit()
        session.refresh(task)
    outcome = asyncio.run(run_service.run_task_now(session, task))
    session.refresh(task)
    return outcome


def _events(session, task, event):
    return [e for e in log_service.recent(session, task_id=task.id, limit=500) if e.event == event]


def _last_task_status(session, task, needle):
    for entry in log_service.recent(session, task_id=task.id, limit=500):
        if entry.event == "task_status" and needle in entry.details:
            return entry
    raise AssertionError(f"no task_status event containing {needle!r}")


def _cancel_soon(task_id, delay=0.3):
    def cancel():
        time.sleep(delay)
        from orchestrator.core.db import session_scope

        with session_scope() as s:
            run_service.cancel(s, task_service.get(s, task_id))

    threading.Thread(target=cancel).start()


def test_three_consecutive_timeouts_reach_blocked_on_the_attempt_limit(
    session, project, monkeypatch
):
    monkeypatch.setattr(mock_harness, "MOCK_TIMEOUT_SECONDS", 0.4)
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 3})
    task = _task(session, project, ["mock-timeout"])

    for attempt in range(1, 4):
        outcome = _run(session, task)
        assert outcome["status"] == "timeout"
        if attempt < 3:
            assert task.status == "planned"

    assert task.status == "blocked"
    stats = supervisor_service.run_attempt_stats(session, task)
    assert stats == {
        "completed": 0,
        "timeout": 3,
        "failed": 0,
        "cancelled": 0,
        "attempts": 3,
        "verified_rounds": 0,
    }

    blocked = _last_task_status(session, task, "-> blocked")
    assert "run attempt limit (max_run_attempts_per_task=3)" in blocked.details
    assert "attempts=3 (completed=0, timeout=3, failed=0, cancelled=0)" in blocked.details
    assert "review round limit" not in blocked.details

    limits = _events(session, task, "task_limits_exhausted")
    assert len(limits) == 1
    payload = structured_log.parse(limits[0].details)
    assert payload is not None
    assert payload["limits"] == ["attempts"]
    assert payload["attempt_limit"] == 3 and payload["attempts"] == 3


def test_default_attempt_limit_preserves_the_completed_only_flow(session, project):
    # DoD 5: the chosen default is explicit, and it does not change a flow that only ever submits.
    assert settings_service.defaults()["max_run_attempts_per_task"] == 6
    assert settings_service.get(session, "max_run_attempts_per_task") == 6
    settings_service.update(session, {"max_review_rounds": 2})
    task = _task(session, project, ["mock", "mock-reject"])

    first = _run(session, task)
    assert first["verdict"] == "CHANGES_REQUESTED" and task.status == "queued"
    second = _run(session, task)
    assert second["verdict"] == "CHANGES_REQUESTED" and task.status == "blocked"

    blocked = _last_task_status(session, task, "-> blocked")
    assert "review round limit (max_review_rounds=2)" in blocked.details
    assert "attempts=2 (completed=2, timeout=0, failed=0, cancelled=0)" in blocked.details
    assert "run attempt limit" not in blocked.details
    stats = supervisor_service.run_attempt_stats(session, task)
    assert stats["attempts"] == 2 and stats["verified_rounds"] == 2


def test_mixed_sequence_counts_correctly_against_both_limits(session, project, monkeypatch):
    monkeypatch.setattr(mock_harness, "MOCK_TIMEOUT_SECONDS", 1.0)
    settings_service.update(session, {"max_review_rounds": 2, "max_run_attempts_per_task": 4})
    task = _task(session, project, ["mock-timeout"])

    assert _run(session, task)["status"] == "timeout"
    assert task.status == "planned"
    timeout_requeue = _last_task_status(session, task, "in_progress -> planned")
    assert "previous round submission" in timeout_requeue.details
    assert "absent (run ended timeout without a usable handoff)" in timeout_requeue.details

    _cancel_soon(task.id)
    assert _run(session, task, ["mock-timeout"])["status"] == "cancelled"
    assert task.status == "planned"
    cancel_requeue = _last_task_status(session, task, "in_progress -> planned")
    assert "previous round submission" in cancel_requeue.details
    assert "absent (run ended cancelled without a usable handoff)" in cancel_requeue.details

    outcome = _run(session, task, ["mock", "mock-reject"])
    assert outcome["verdict"] == "CHANGES_REQUESTED" and task.status == "queued"
    assert supervisor_service.run_attempt_stats(session, task) == {
        "completed": 1,
        "timeout": 1,
        "failed": 0,
        "cancelled": 1,
        "attempts": 3,
        "verified_rounds": 1,
    }

    assert _run(session, task, ["mock-timeout"])["status"] == "timeout"
    assert task.status == "blocked"
    stats = supervisor_service.run_attempt_stats(session, task)
    assert stats == {
        "completed": 1,
        "timeout": 2,
        "failed": 0,
        "cancelled": 1,
        "attempts": 4,
        "verified_rounds": 1,
    }

    blocked = _last_task_status(session, task, "in_progress -> blocked")
    assert "run attempt limit (max_run_attempts_per_task=4)" in blocked.details
    assert (
        "attempts=4 (completed=1, timeout=2, failed=0, cancelled=1); verified rounds=1"
        in blocked.details
    )
    assert "previous round submission" in blocked.details
    assert "absent (run ended timeout without a usable handoff)" in blocked.details
    payload = structured_log.parse(_events(session, task, "task_limits_exhausted")[0].details)
    assert payload is not None and payload["limits"] == ["attempts"]


def test_cancelled_run_at_the_attempt_limit_blocks_with_its_submission_status(
    session, project, monkeypatch
):
    monkeypatch.setattr(mock_harness, "MOCK_TIMEOUT_SECONDS", 1.0)
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 2})
    task = _task(session, project, ["mock-timeout"])

    assert _run(session, task)["status"] == "timeout"
    assert task.status == "planned"

    _cancel_soon(task.id)
    assert _run(session, task, ["mock-timeout"])["status"] == "cancelled"
    assert task.status == "blocked"

    blocked = _last_task_status(session, task, "in_progress -> blocked")
    assert "run cancelled" in blocked.details
    assert "run attempt limit (max_run_attempts_per_task=2)" in blocked.details
    assert "attempts=2 (completed=0, timeout=1, failed=0, cancelled=1)" in blocked.details
    assert "previous round submission" in blocked.details
    assert "absent (run ended cancelled without a usable handoff)" in blocked.details
    payload = structured_log.parse(_events(session, task, "task_limits_exhausted")[0].details)
    assert payload is not None and payload["limits"] == ["attempts"]
    assert payload["cancelled"] == 1 and payload["attempts"] == 2


def test_failed_run_at_the_attempt_limit_blocks_with_its_submission_status(session, project):
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 2})
    task = _task(session, project, ["mock-fail"])

    assert _run(session, task)["status"] == "failed"
    assert task.status == "planned"
    planned = _last_task_status(session, task, "in_progress -> planned")
    assert "previous round submission" in planned.details
    assert "absent (run ended failed without a usable handoff)" in planned.details

    assert _run(session, task)["status"] == "failed"
    assert task.status == "blocked"
    blocked = _last_task_status(session, task, "in_progress -> blocked")
    assert "run failed" in blocked.details
    assert "run attempt limit (max_run_attempts_per_task=2)" in blocked.details
    assert "attempts=2 (completed=0, timeout=0, failed=2, cancelled=0)" in blocked.details
    assert "previous round submission" in blocked.details
    assert "absent (run ended failed without a usable handoff)" in blocked.details


def test_unexpected_exception_at_the_attempt_limit_blocks(session, project, monkeypatch):
    monkeypatch.setattr(mock_harness, "MOCK_TIMEOUT_SECONDS", 0.4)
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 2})
    task = _task(session, project, ["mock-timeout"])
    assert _run(session, task)["status"] == "timeout"
    assert task.status == "planned"

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash after the run row existed")

    monkeypatch.setattr(run_service, "_execute", boom)
    outcome = _run(session, task, ["mock"])
    assert outcome["status"] == "failed"
    assert "simulated crash after the run row existed" in outcome["error"]
    assert task.status == "blocked"

    blocked = _last_task_status(session, task, "in_progress -> blocked")
    assert "run failed: simulated crash after the run row existed" in blocked.details
    assert "run attempt limit (max_run_attempts_per_task=2)" in blocked.details
    assert "attempts=2 (completed=0, timeout=1, failed=1, cancelled=0)" in blocked.details
    assert "previous round submission" in blocked.details
    assert "absent (run ended failed without a usable handoff)" in blocked.details


def test_provider_error_at_the_attempt_limit_blocks(session, project):
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 2})
    task = _task(session, project, ["mock", "mock-provider-error"], kind="agent")

    first = _run(session, task)
    assert first["status"] == "failed"
    assert "mock provider simulated failure" in first["error"]
    assert task.status == "planned"
    planned = _last_task_status(session, task, "in_progress -> planned")
    assert "previous round submission" in planned.details
    assert "absent (run ended failed without a usable handoff)" in planned.details

    second = _run(session, task)
    assert second["status"] == "failed"
    assert task.status == "blocked"
    blocked = _last_task_status(session, task, "in_progress -> blocked")
    assert "provider error: mock provider simulated failure" in blocked.details
    assert "run attempt limit (max_run_attempts_per_task=2)" in blocked.details
    assert "attempts=2 (completed=0, timeout=0, failed=2, cancelled=0)" in blocked.details
    assert "previous round submission" in blocked.details
    assert "absent (run ended failed without a usable handoff)" in blocked.details


def test_changes_requested_requeue_names_the_previous_submission(session, project):
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 6})
    task = _task(session, project, ["mock", "mock-reject"])
    _run(session, task)
    assert task.status == "queued"

    queued = _last_task_status(session, task, "-> queued")
    assert "previous round submission" in queued.details
    assert "submitted on clean exit" in queued.details


def test_next_prompt_states_an_absent_previous_submission(
    session, project, monkeypatch
):
    monkeypatch.setattr(mock_harness, "MOCK_TIMEOUT_SECONDS", 0.4)
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 6})
    task = _task(session, project, ["mock-timeout"])

    assert _run(session, task)["status"] == "timeout"
    assert task.status == "planned"

    outcome = _run(session, task, ["mock"])
    prompt = artifact_service.read_text(outcome["run_id"], "prompt.md")
    assert prompt is not None
    assert "Previous round submission status" in prompt
    assert "absent (run ended timeout without a usable handoff)" in prompt
    assert "No handoff.json was present in the worktree when this round started." in prompt


def test_salvaged_round_is_named_in_the_requeue_and_the_next_prompt(session, project):
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 6})
    task = _task(session, project, ["mock", "mock-reject", "mock-timeout-handoff"])

    first = _run(session, task)
    assert first["status"] == "timeout" and first["salvaged"] is True
    assert first["verdict"] == "CHANGES_REQUESTED" and task.status == "queued"

    queued = _last_task_status(session, task, "-> queued")
    assert "previous round submission" in queued.details
    assert "salvaged from a killed run" in queued.details

    outcome = _run(session, task, ["mock"])
    prompt = artifact_service.read_text(outcome["run_id"], "prompt.md")
    assert prompt is not None
    assert "Previous round submission status" in prompt
    assert "salvaged from a killed run" in prompt
    # T-10 changed the old round-start delete into archive-then-unlink; the prompt must say which.
    assert "archived to the orchestrator's evidence store" in prompt


def test_task_api_exposes_attempts_in_the_list_and_detail(session, project, client):
    settings_service.update(session, {"max_review_rounds": 3, "max_run_attempts_per_task": 6})
    task = _task(session, project, ["mock", "mock-reject"])
    outcome = _run(session, task)
    assert session.get(Run, outcome["run_id"]).status == "completed"

    detail = client.get(f"/api/tasks/{task.id}").json()
    assert detail["attempts"]["summary"] == "1 attempt, 1 verified round"
    assert detail["attempts"]["max_review_rounds"] == 3
    assert detail["attempts"]["max_run_attempts_per_task"] == 6

    listed = client.get("/api/tasks").json()
    row = next(t for t in listed if t["id"] == task.id)
    assert row["attempts"] == detail["attempts"]
