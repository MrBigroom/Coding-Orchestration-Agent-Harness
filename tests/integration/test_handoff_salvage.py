"""T-10 end to end: a run that is killed AFTER the Worker wrote its handoff must not lose the
round. Offline: mock harness + mock provider against a real local git repo. No network, no live
service, no real provider.

Measured on run 11 (T-8 round 2, 2026-11-04): a 36184-byte `handoff.json` validated cleanly and was
never read, because `run_service.execute` returned early on the non-clean exit and the next round
unlinked the file. These tests pin the opposite behaviour for every non-clean exit shape
(timeout, cancel, non-zero exit, harness-reported error), the two fallbacks (no file, unusable
file), the retry that used to destroy the previous submission, and the claims-vs-commits evidence
that stops a salvaged promise reading as delivered work.
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from sqlmodel import select

from orchestrator.core.models import Review, Run, TaskStatus, Verification
from orchestrator.core.schemas import Handoff, ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
    log_service,
    project_service,
    prompt_builder,
    review_service,
    run_service,
    settings_service,
    supervisor_service,
    task_service,
)
from orchestrator.utils import structured_log
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

RUN_TESTS = "import sys; print('3 passed, 0 failed'); sys.exit(0)\n"
LINT_NAMES = '''"""Report names that are used but never defined (stdlib-only undefined-name lint)."""
import ast
import builtins
import pathlib
import sys

SELF = pathlib.Path(__file__).name
bad = []
for f in sorted(pathlib.Path(".").glob("*.py")):
    if f.name == SELF:
        continue
    tree = ast.parse(f.read_text(), str(f))
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                defined.update(x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs))
                if a.vararg:
                    defined.add(a.vararg.arg)
                if a.kwarg:
                    defined.add(a.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.Import):
            defined.update((n.asname or n.name).split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            defined.update(n.asname or n.name for n in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined:
                bad.append(f"{f}:{node.lineno}: F821 undefined name {node.id!r}")
for line in bad:
    print(line)
sys.exit(1 if bad else 0)
'''


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(
        tmp_path / "proj",
        {
            "run_tests.py": RUN_TESTS,
            "lint_names.py": LINT_NAMES,
            "README.md": "x\n",
            "docs/frozen-contracts.md": "# frozen contracts\n",
        },
    )
    return project_service.create(
        session,
        ProjectCreate(
            name="proj",
            path=str(repo),
            test_command="python3 run_tests.py",
            lint_command="python3 lint_names.py",
        ),
    )


def _task(session, project, tags, phase, dod=("MOCK_CHANGE.md updated",)):
    # Every task carries the plain `mock` tag as well as its behaviour tag: provider routing only
    # selects the offline mock provider when `"mock"` is a tag (the behaviour tags such as
    # `mock-timeout-handoff` match the harness, not `"mock"`), so without it the Architect review
    # would call the configured `architect-claude-cli`/Opus profile — a real external CLI inside a
    # test, non-deterministic (APPROVED vs CHANGES_REQUESTED on identical inputs) and slow.
    return task_service.create(
        session,
        TaskCreate(
            title=f"T-10 salvage probe {phase}",
            project_id=project.id,
            tags=[*tags, "mock"],
            phase_number=phase,
            phase_slug=f"p{phase}",
            definition_of_done=list(dod),
        ),
    )


def _run(session, task):
    outcome = asyncio.run(run_service.run_task_now(session, task))
    session.refresh(task)
    return outcome


def _events(session, task, event):
    return [e for e in log_service.recent(session, task_id=task.id, limit=400) if e.event == event]


def _checks(run_id) -> dict:
    return {
        c["name"]: c
        for c in json.loads(artifact_service.read_text(run_id, "verification.json"))["checks"]
    }


def _cancel_soon(task_id: int, delay: float = 1.2) -> threading.Thread:
    """Cancel from another thread, the way the API/GUI cancel button does (cross-process marker).

    The delay sits between the mock's own startup+write (~0.4s) and its clamped 2s timeout, so the
    run is cancelled with the submission already on disk — the exact shape measured live at 01:33:05
    while cancelling run 13.
    """

    def go():
        time.sleep(delay)
        from orchestrator.core.db import session_scope

        with session_scope() as s:
            run_service.cancel(s, task_service.get(s, task_id))

    t = threading.Thread(target=go)
    t.start()
    return t


# ------------------------------------------------------------------ DoD 1, 2, 4: the salvage


def test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate(session, project):
    task = _task(session, project, ["mock-timeout-handoff"], 1)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])

    # the round is not lost, and the run's terminal status is NOT widened
    assert outcome["status"] == "timeout" and run.status == "timeout", outcome
    assert outcome.get("salvaged") is True
    assert run.finished_at is not None and run.commit_sha

    # the submission reached the frozen artifact names
    names = {a["name"] for a in artifact_service.list_artifacts(run.id)}
    assert {"handoff.json", "handoff.md", "patch.diff", "diffstat.txt", "run.json"} <= names, names
    archived = Handoff.model_validate(
        json.loads(artifact_service.read_text(run.id, "handoff.json"))
    )
    assert archived.status == "READY_FOR_REVIEW" and archived.files_changed == ["MOCK_CHANGE.md"]
    assert "# Handoff —" in artifact_service.read_text(run.id, "handoff.md")

    # verification AND review rows are created — the gate ran on the salvaged submission
    assert len(session.exec(select(Verification)).all()) == 1
    assert len(session.exec(select(Review)).all()) == 1
    assert outcome.get("verification") == "pass", outcome
    assert outcome.get("verdict") == "APPROVED"

    checks = _checks(run.id)
    assert checks["handoff present"]["status"] == "pass"
    assert checks[supervisor_service.SALVAGE_CHECK]["status"] == "pass"
    assert "killed" in checks[supervisor_service.SALVAGE_CHECK]["notes"]
    assert checks[supervisor_service.CLAIMS_CHECK]["status"] == "pass"
    assert checks["tests"]["status"] == "pass"  # the T-6 wrapper path is untouched

    # lifecycle representation (DoD 4): SALVAGED, distinguishable, routed to a human
    assert task.handoff_status == "SALVAGED"
    assert task.handoff_status not in {"SYNTHESIZED", "READY_FOR_REVIEW", "RUN_TIMEOUT"}
    assert task.review_status == "NEEDS_HUMAN"
    assert task.status == TaskStatus.blocked  # waiting for a decision, not "the round failed"
    assert task.merge_status != "merged" and settings_service.get(session, "merge_enabled") is False

    # the new additive event, with the run exit state and why it arrived out of band
    events = _events(session, task, "handoff_salvaged")
    assert len(events) == 1, [e.details for e in events]
    payload = structured_log.parse(events[0].details)
    assert payload is not None
    assert events[0].run_id == run.id and events[0].commit_sha == run.commit_sha
    assert payload["mode"] == "non_clean_exit"
    assert payload["run_status"] == "timeout" and payload["timed_out"] is True
    assert payload["salvaged"] is True and payload["handoff_status"] == "READY_FOR_REVIEW"
    assert payload["claims_all_backed_by_worker_commits"] is True
    summary = structured_log.summary_of(events[0].details)
    assert "out of band" in summary and "run.status stays timeout" in summary
    assert _events(session, task, "handoff_salvage_failed") == []

    # and the operator can take the decision: the round is recoverable, not burned
    decision = run_service.human_decision(session, task, True, "operator accepts the salvaged round")
    session.refresh(task)
    assert decision["decision"] == "accepted" and task.status == TaskStatus.accepted
    assert task.merge_status == "ready"  # merge command offered, never auto-merged


def test_cancel_with_a_valid_handoff_is_salvaged(session, project):
    task = _task(session, project, ["mock-cancel-handoff"], 2)
    thread = _cancel_soon(task.id or 0)
    outcome = _run(session, task)
    thread.join(timeout=5)
    run = session.get(Run, outcome["run_id"])

    assert outcome["status"] == "cancelled" and run.status == "cancelled", outcome
    assert outcome.get("salvaged") is True
    assert task.handoff_status == "SALVAGED"
    assert json.loads(artifact_service.read_text(run.id, "handoff.json"))["status"] == (
        "READY_FOR_REVIEW"
    )
    assert len(session.exec(select(Verification)).all()) == 1
    assert len(session.exec(select(Review)).all()) == 1
    payload = structured_log.parse(_events(session, task, "handoff_salvaged")[0].details)
    assert payload["cancelled"] is True and payload["run_status"] == "cancelled"
    # the frozen vocabulary is untouched: T-9 owns `run_cancelled`, this task adds it to nothing
    assert "run_cancelled" not in [e.event for e in log_service.recent(session, limit=400)]


def test_non_zero_exit_with_a_valid_handoff_is_salvaged(session, project):
    task = _task(session, project, ["mock-fail-handoff"], 3)
    outcome = _run(session, task)
    assert outcome["status"] == "failed" and outcome.get("salvaged") is True
    assert task.handoff_status == "SALVAGED" and task.status == TaskStatus.blocked
    assert task.review_status == "NEEDS_HUMAN"
    events = _events(session, task, "handoff_salvaged")
    assert len(events) == 1
    payload = structured_log.parse(events[0].details)
    assert payload["exit_code"] == 1 and payload["timed_out"] is False
    assert "run_timeout" not in [e.event for e in log_service.recent(session, limit=400)]


# ------------------------------------------------------- DoD 5(b), (c): today's behaviour kept


def test_timeout_with_no_handoff_is_exactly_todays_behaviour(session, project):
    task = _task(session, project, ["mock-timeout"], 4)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])
    assert outcome["status"] == "timeout" and "salvaged" not in outcome
    assert task.handoff_status == "RUN_TIMEOUT"
    assert task.status == TaskStatus.planned
    assert task.review_status is None and task.verification_status is None
    # nothing was archived, nothing was invented, the gate never ran
    names = {a["name"] for a in artifact_service.list_artifacts(run.id)}
    assert "handoff.json" not in names and "handoff.md" not in names, names
    assert session.exec(select(Verification)).first() is None
    assert session.exec(select(Review)).first() is None
    assert _events(session, task, "handoff_salvaged") == []
    assert _events(session, task, "handoff_salvage_failed") == []
    assert len(_events(session, task, "run_timeout")) == 1


def test_timeout_with_a_truncated_handoff_falls_back_and_logs_the_parse_failure(
    session, project
):
    task = _task(session, project, ["mock-timeout-bad-handoff"], 5)
    outcome = _run(session, task)  # it must not raise: a half-written file is the normal shape
    run = session.get(Run, outcome["run_id"])

    assert outcome["status"] == "timeout" and "salvaged" not in outcome
    assert task.handoff_status == "RUN_TIMEOUT" and task.status == TaskStatus.planned
    assert session.exec(select(Verification)).first() is None
    names = {a["name"] for a in artifact_service.list_artifacts(run.id)}
    assert "handoff.json" not in names, names  # garbage is not laundered into a handoff
    failures = _events(session, task, "handoff_salvage_failed")
    assert len(failures) == 1, [e.details for e in failures]
    assert "not valid JSON" in failures[0].details
    assert structured_log.parse(failures[0].details)["salvaged"] is False
    assert _events(session, task, "handoff_salvaged") == []


# ------------------------------------------------------------- DoD 3: never discard at round start


def test_a_pre_existing_submission_is_archived_before_the_round_start_unlink(session, project):
    task = _task(session, project, ["mock-timeout"], 6)
    first = _run(session, task)
    run1 = session.get(Run, first["run_id"])
    wt = Path(run1.worktree_path)

    # the run-11 shape: a killed round left a valid submission nobody ingested
    submission = {
        "phase": "phase-6-p6",
        "branch": task.branch_name,
        "status": "READY_FOR_REVIEW",
        "summary": "written by the killed round, never ingested",
        "files_changed": ["README.md"],
    }
    (wt / ".orchestrator" / "handoff.json").write_text(json.dumps(submission, indent=2))
    bytes_before = (wt / ".orchestrator" / "handoff.json").read_bytes()

    second = _run(session, task)
    run2 = session.get(Run, second["run_id"])

    archived = _events(session, task, "handoff_salvaged")
    assert len(archived) == 1, [e.details for e in archived]
    payload = structured_log.parse(archived[0].details)
    assert payload["mode"] == "pre_round_archive"
    assert payload["valid"] is True and payload["source_run"] == run1.short_id
    assert payload["source_run_status"] == "timeout"
    dest = Path(payload["archived_to"])
    assert dest.is_dir() and (dest / "handoff.json").read_bytes() == bytes_before
    assert "never ingested" in (dest / "handoff.md").read_text()
    # the new round is unaffected: it produced NO submission of its own (the `mock-timeout` tag
    # writes nothing), and the leftover was never ingested as this round's handoff
    assert second["status"] == "timeout" and "salvaged" not in second
    assert artifact_service.read_text(run2.id, "handoff.json") is None
    assert run2.id != run1.id


def test_an_unusable_leftover_is_archived_raw_and_the_round_still_starts(session, project):
    task = _task(session, project, ["mock-timeout"], 7)
    first = _run(session, task)
    wt = Path(session.get(Run, first["run_id"]).worktree_path)
    (wt / ".orchestrator" / "handoff.json").write_text('{"phase": "x", "summary": "trunc')

    second = _run(session, task)
    # the round still starts and ends on its own terms: the unusable leftover is archived raw and
    # the `mock-timeout` tag's plain timeout is reported exactly as before
    assert second["status"] == "timeout" and "salvaged" not in second, second
    payload = structured_log.parse(_events(session, task, "handoff_salvaged")[0].details)
    assert payload["valid"] is False and "not valid JSON" in payload["parse_error"]
    assert (Path(payload["archived_to"]) / "handoff.json").read_text().startswith('{"phase"')
    assert not (Path(payload["archived_to"]) / "handoff.md").exists()


def test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy(session, project):
    task = _task(session, project, ["mock-timeout-handoff"], 8)
    first = _run(session, task)
    run1 = session.get(Run, first["run_id"])
    archived_path = artifact_service.run_dir(run1.id) / "handoff.json"
    bytes_before = archived_path.read_bytes()
    assert json.loads(bytes_before)["status"] == "READY_FOR_REVIEW"

    # the operator retries the round on the same branch without accepting it (blocked -> queued is
    # the documented retry edge; `accepted -> queued` is not a legal transition)
    task = task_service.get(session, task.id)
    assert task.status == TaskStatus.blocked and task.handoff_status == "SALVAGED"
    task_service.queue(session, task)
    second = _run(session, task)
    session.refresh(task)

    assert second["run_id"] != run1.id
    assert archived_path.read_bytes() == bytes_before  # the salvaged copy survives the retry
    assert json.loads(artifact_service.read_text(second["run_id"], "handoff.json"))
    # the leftover from round 1 was already archived in runs/<run1>/handoff.json, byte-identical,
    # so the round-start unlink is safe without a second copy in the evidence root
    assert [e for e in _events(session, task, "handoff_salvaged") if "pre_round_archive" in e.details] == []


# ------------------------------------------------------ DoD 7: a promise reads as a promise


def test_salvaged_claims_that_were_never_committed_fail_the_claims_check(session, project):
    task = _task(session, project, ["mock-timeout-promise-handoff"], 9)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])

    # the submission exists and is salvaged (so the round is not thrown away)...
    assert run.status == "timeout" and task.handoff_status == "SALVAGED"
    assert json.loads(artifact_service.read_text(run.id, "handoff.json"))["files_changed"] == [
        "PROMISE_ONLY.md"
    ]
    # ...but the harness proves it was never the Worker's commit: the file arrived only as
    # uncommitted dirt that the recovery sweep had to commit, so it is a promise, and a promise
    # does not pass the gate (DoD 6: an incomplete round is still reported incomplete).
    checks = _checks(run.id)
    assert checks[supervisor_service.CLAIMS_CHECK]["status"] == "fail", checks
    assert "PROMISE_ONLY.md=recovery_commit_only" in checks[supervisor_service.CLAIMS_CHECK]["notes"]
    assert outcome["verification"] == "fail", outcome
    # The gate believed the deterministic check, not the promise: the round was requeued and never
    # accepted. The canned mock reviewer's verdict is irrelevant here — verification decides —
    # which is exactly the property that must hold when a salvaged handoff over-promises.
    assert task.status == TaskStatus.queued
    assert task.verification_status == "fail"
    assert task.merge_status != "ready"
    for event in _events(session, task, "verdict_recorded"):
        assert "accepted" not in event.details

    evidence = artifact_service.handoff_delivery_evidence(run.id)
    assert artifact_service.handoff_arrival(run.id) == "salvaged"
    assert evidence["claims_all_backed_by_worker_commits"] is False
    assert evidence["claim_status"] == {"PROMISE_ONLY.md": "recovery_commit_only"}
    # the DoD 7 payload: commit list, diffstat, porcelain at ingestion, and the pre-sweep dirt that
    # proves the claim existed only in the worktree
    assert evidence["uncommitted_before_harness_sweep"] == ["PROMISE_ONLY.md"]
    assert evidence["porcelain_at_ingestion"] == []  # clean only because the harness swept it
    assert evidence["recovery_commit_count"] == 1 and evidence["worker_commit_count"] == 0
    assert "PROMISE_ONLY.md" in evidence["diffstat"]
    assert evidence["commits"][0]["recovery"] is True

    text = artifact_service.render_delivery_evidence_md(evidence, "salvaged")
    assert "handoff arrival: salvaged" in text
    assert "PROMISE_ONLY.md -> recovery_commit_only" in text
    assert "claims_all_backed_by_worker_commits=false" in text
    assert "git status --porcelain -uall at ingestion: clean" in text


def test_the_clean_exit_path_carries_the_same_evidence(session, project):
    task = _task(session, project, ["mock"], 10)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])

    assert artifact_service.handoff_arrival(run.id) == "clean_exit"
    assert task.handoff_status == "READY_FOR_REVIEW" and task.status == TaskStatus.accepted
    evidence = artifact_service.handoff_delivery_evidence(run.id)
    assert evidence["claims_all_backed_by_worker_commits"] is True
    assert evidence["claim_status"] == {"MOCK_CHANGE.md": "worker_commit"}
    assert evidence["worker_commit_count"] == 1 and evidence["recovery_commit_count"] == 0
    assert evidence["exit_state"].startswith(f"run {run.short_id} status=completed")
    checks = _checks(run.id)
    assert supervisor_service.SALVAGE_CHECK not in checks  # nothing was salvaged
    assert checks[supervisor_service.CLAIMS_CHECK]["status"] == "pass"

    # the reviewer is shown the git facts, from the archived artifact, not from the Worker's prose
    section = review_service.delivery_evidence_section(session, task, run)
    assert "handoff arrival: clean_exit" in section
    assert "MOCK_CHANGE.md -> worker_commit" in section
    assert "diffstat vs merge-base" in section
    rendered = prompt_builder._dynamic(prompt_builder.DynamicState(delivery_evidence=section))
    assert "### Handoff provenance and delivery evidence" in rendered


def test_a_run_with_no_archived_evidence_says_so(session, project):
    """A reviewer payload for a run predating T-10 (or an agent task) must not read as "no
    claims were made": it states that nothing was archived."""
    task = _task(session, project, ["mock"], 12)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])
    assert artifact_service.handoff_arrival(run.id) == "clean_exit"
    stale = artifact_service.run_dir(run.id) / "run.json"
    stale.write_text(json.dumps({"run": run.short_id}), encoding="utf-8")
    assert artifact_service.handoff_arrival(run.id) is None
    assert artifact_service.handoff_delivery_evidence(run.id) is None
    assert "no delivery evidence was archived" in review_service.delivery_evidence_section(
        session, task, run
    )
