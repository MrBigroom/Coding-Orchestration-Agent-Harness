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

from orchestrator.adapters.base import AdapterResult
from orchestrator.adapters.mock_harness import MockHarnessAdapter
from orchestrator.core.models import Review, Run, TaskStatus, Verification
from orchestrator.core.schemas import Handoff, ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
    git_service,
    log_service,
    project_service,
    prompt_builder,
    provider_service,
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


def _cancel_when_handoff_written(task_id: int, worktree: Path, timeout: float = 12.0) -> threading.Thread:
    """Cancel once the submission is actually on disk, not after a guessed sleep.

    The measured shape is a cancelled run whose handoff was already written. Polling for the file
    removes the race the old 1.2s sleep had with the mock's startup; the mock's own cancel clamp
    (15s) is only a safety net if this thread fails, not the thing that ends the run.
    """
    handoff = worktree / ".orchestrator" / "handoff.json"

    def go():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if handoff.is_file() and handoff.stat().st_size > 0:
                    break
            except OSError:
                pass
            time.sleep(0.02)
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
    assert "handoff claims backed by Worker commits" not in checks  # provenance, not a new gate
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
    assert payload["claims_all_backed_by_this_run_commits"] is True
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
    worktree = git_service.worktree_path(project.slug, task.id)
    thread = _cancel_when_handoff_written(task.id or 0, worktree)
    outcome = _run(session, task)
    thread.join(timeout=10)
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


def test_a_stale_leftover_is_not_salvaged_as_the_new_rounds_submission(
    session, project, monkeypatch
):
    """The round-start archive can fail (ORDINATOR_HOME/evidence unwritable).

    The stale file then stays in the worktree. A killed round must NOT ingest it: it is the
    previous round's submission, and attributing it to this run would be exactly the provenance
    failure the salvage path exists to avoid. Fall back to today's behaviour instead.
    """
    task = _task(session, project, ["mock-timeout"], 11)
    first = _run(session, task)
    wt = Path(session.get(Run, first["run_id"]).worktree_path)
    leftover = {
        "phase": "phase-11-p11",
        "branch": task.branch_name,
        "status": "READY_FOR_REVIEW",
        "summary": "previous round's submission, never ingested",
        "files_changed": ["README.md"],
    }
    (wt / ".orchestrator" / "handoff.json").write_text(json.dumps(leftover, indent=2))

    # the evidence root is unavailable, so archive_before_round_start must leave the file in place
    def unavailable(kind, key):
        raise OSError("evidence root unavailable")

    monkeypatch.setattr(artifact_service, "evidence_dir", unavailable)

    second = _run(session, task)
    run2 = session.get(Run, second["run_id"])
    assert second["status"] == "timeout" and "salvaged" not in second
    assert task.handoff_status == "RUN_TIMEOUT" and task.status == TaskStatus.planned
    assert artifact_service.read_text(run2.id, "handoff.json") is None
    assert session.exec(select(Verification)).first() is None
    failures = [
        structured_log.parse(e.details)
        for e in _events(session, task, "handoff_salvage_failed")
        if e.run_id == run2.id
    ]
    assert any(p and p.get("stale") is True for p in failures), failures
    assert any(
        p and "unchanged since round start" in (p.get("reason") or "") for p in failures
    ), failures
    assert [
        e
        for e in _events(session, task, "handoff_salvaged")
        if e.run_id == run2.id and "non_clean_exit" in e.details
    ] == []
    # the leftover is still on disk untouched: refusing to read it is not permission to delete it
    assert json.loads((wt / ".orchestrator" / "handoff.json").read_text())["summary"] == (
        "previous round's submission, never ingested"
    )


def test_a_clean_exit_does_not_ingest_a_stale_leftover_and_synthesizes(
    session, project, monkeypatch
):
    """The clean-exit half of the same provenance guard (DoD 3, DoD 6, DoD 7).

    When the round-start archive fails, the fail-safe is to leave the previous round's file in
    the worktree. A clean exit that never replaced it must then synthesize from observed evidence
    instead of reading the leftover as this round's submission -- otherwise the previous round's
    work would be self-certified and could be auto-accepted. The stale branch has to exist on the
    clean path too, not only on the salvage path.
    """
    task = _task(session, project, ["mock-timeout"], 14)
    first = _run(session, task)  # creates the worktree; this tag writes no handoff
    wt = Path(session.get(Run, first["run_id"]).worktree_path)
    leftover = {
        "phase": "phase-14-p14",
        "branch": task.branch_name,
        "status": "READY_FOR_REVIEW",
        "summary": "previous round's submission, never replaced",
        "files_changed": ["README.md"],
    }
    (wt / ".orchestrator" / "handoff.json").write_text(json.dumps(leftover, indent=2))

    # the archive cannot copy the leftover out, so it stays in place ...
    def unavailable(kind, key):
        raise OSError("evidence root unavailable")

    monkeypatch.setattr(artifact_service, "evidence_dir", unavailable)

    # ... and the next round exits cleanly WITHOUT writing a new handoff of its own
    async def clean_exit_no_handoff(self, ctx, runner):
        return AdapterResult(exit_code=0)

    monkeypatch.setattr(MockHarnessAdapter, "run", clean_exit_no_handoff)
    second = _run(session, task)
    run2 = session.get(Run, second["run_id"])

    assert run2.status == "completed"
    # the leftover was neither ingested nor deleted: the round synthesized from observed evidence
    assert artifact_service.handoff_arrival(run2.id) == "synthesized"
    archived = json.loads(artifact_service.read_text(run2.id, "handoff.json"))
    assert "synthesized by the orchestrator" in archived["summary"]
    assert archived["summary"] != leftover["summary"]
    failures = [
        structured_log.parse(e.details)
        for e in _events(session, task, "handoff_salvage_failed")
        if e.run_id == run2.id
    ]
    assert any(p and p.get("stale") is True for p in failures), failures
    assert any(
        p and "unchanged since round start" in (p.get("reason") or "") for p in failures
    ), failures
    assert json.loads((wt / ".orchestrator" / "handoff.json").read_text())["summary"] == (
        leftover["summary"]
    )


# ------------------------------------------------------ DoD 7: a promise reads as a promise


def test_a_salvaged_promise_is_flagged_in_the_evidence_not_turned_into_a_new_gate(
    session, project
):
    task = _task(session, project, ["mock-timeout-promise-handoff"], 9)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])

    # the submission exists and is salvaged (so the round is not thrown away)...
    assert run.status == "timeout" and task.handoff_status == "SALVAGED"
    assert json.loads(artifact_service.read_text(run.id, "handoff.json"))["files_changed"] == [
        "PROMISE_ONLY.md"
    ]
    # ...but it was never a Worker commit: the file arrived only as uncommitted dirt that the
    # recovery sweep had to commit. DoD 7 asks for that fact to be ATTACHED (artifacts + reviewer
    # payload) and for a structured flag, not for a new gate-failing check: a harness recovery
    # commit is still content on the branch and T-8 owns how recovery commits are treated.
    checks = _checks(run.id)
    assert "handoff claims backed by Worker commits" not in checks, checks
    assert outcome["verification"] == "pass", outcome

    evidence = artifact_service.handoff_delivery_evidence(run.id)
    assert artifact_service.handoff_arrival(run.id) == "salvaged"
    assert evidence["claims_all_backed_by_this_run_commits"] is False
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
    assert "claims_all_backed_by_this_run_commits=false" in text
    assert "git status --porcelain -uall at ingestion: clean" in text

    # The promise is visible, and the round still cannot auto-complete: a salvaged submission is
    # routed to a human, so a reviewer reads the flag before anything is accepted or merged.
    assert task.status == TaskStatus.blocked
    assert task.review_status == "NEEDS_HUMAN"
    assert task.merge_status != "ready"
    for event in _events(session, task, "verdict_recorded"):
        assert "accepted" not in event.details


def test_the_architect_prompt_for_a_salvaged_run_carries_the_delivery_evidence(
    session, project, monkeypatch
):
    """DoD 7: the evidence must reach the REVIEWER PAYLOAD, not only the run.json artifact.

    The unit test renders the section; this one captures the actual prompt handed to the architect
    provider for a salvaged run, so the wiring from archived evidence -> review prompt is asserted.
    """
    captured: dict[str, str] = {}
    original = provider_service.complete

    async def capture(session, profile, prompt, metadata=None):
        if (metadata or {}).get("purpose") == "review":
            captured["text"] = prompt.static_text() + "\n\n" + prompt.dynamic_text()
        return await original(session, profile, prompt, metadata)

    monkeypatch.setattr(provider_service, "complete", capture)
    task = _task(session, project, ["mock-timeout-handoff"], 13)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])

    assert outcome.get("verdict") == "APPROVED"
    text = captured["text"]
    assert "### Handoff provenance and delivery evidence" in text
    assert "handoff arrival: salvaged" in text
    assert "MOCK_CHANGE.md -> worker_commit" in text
    assert f"run {run.short_id} status=timeout" in text
    assert "structured flag: claims_all_backed_by_this_run_commits=true" in text
    assert "git status --porcelain -uall at ingestion" in text


def test_the_clean_exit_path_carries_the_same_evidence(session, project):
    task = _task(session, project, ["mock"], 10)
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])

    assert artifact_service.handoff_arrival(run.id) == "clean_exit"
    assert task.handoff_status == "READY_FOR_REVIEW" and task.status == TaskStatus.accepted
    evidence = artifact_service.handoff_delivery_evidence(run.id)
    assert evidence["claims_all_backed_by_this_run_commits"] is True
    assert evidence["claim_status"] == {"MOCK_CHANGE.md": "worker_commit"}
    assert evidence["worker_commit_count"] == 1 and evidence["recovery_commit_count"] == 0
    assert evidence["exit_state"].startswith(f"run {run.short_id} status=completed")
    checks = _checks(run.id)
    assert supervisor_service.SALVAGE_CHECK not in checks  # nothing was salvaged
    assert "handoff claims backed by Worker commits" not in checks  # evidence, not a new gate

    # the reviewer is shown the git facts, from the archived artifact, not from the Worker's prose
    section = review_service.delivery_evidence_section(session, task, run)
    assert "handoff arrival: clean_exit" in section
    assert "MOCK_CHANGE.md -> worker_commit" in section
    assert "diffstat vs merge-base" in section
    rendered = prompt_builder._dynamic(prompt_builder.DynamicState(delivery_evidence=section))
    assert "### Handoff provenance and delivery evidence" in rendered


def test_a_zero_commit_retry_cannot_claim_an_earlier_rounds_files_as_this_runs_delivery(
    session, project, monkeypatch
):
    """reviews.11 finding 1, end to end: scope the claim to the RUN, not the phase branch.

    Round 1 commits MOCK_CHANGE.md. The retry creates zero commits but writes a handoff naming
    the same file. Classifying against the merge-base would call that a Worker commit of this
    round and light `claims_all_backed_by_this_run_commits`; the run baseline (round 1's
    `commit_sha`) must instead report `prior_run_commit` and leave the flag false.
    """
    task = _task(session, project, ["mock-timeout-handoff"], 15)
    first = _run(session, task)
    run1 = session.get(Run, first["run_id"])
    assert first["status"] == "timeout" and run1.commit_sha
    assert task.status == TaskStatus.blocked

    task_service.queue(session, task)
    handoff = {
        "phase": task_service.phase_label(task),
        "branch": task.branch_name,
        "status": "READY_FOR_REVIEW",
        "summary": "claims MOCK_CHANGE.md, but this retry committed nothing",
        "files_changed": ["MOCK_CHANGE.md"],
        "dod_self_check": [{"item": "MOCK_CHANGE.md updated", "met": True, "evidence": ""}],
    }

    async def timeout_without_commits(self, ctx, runner):
        ctx.handoff_path.write_text(json.dumps(handoff, indent=2))
        return AdapterResult(exit_code=None, timed_out=True, error="timed out after 2s")

    monkeypatch.setattr(MockHarnessAdapter, "run", timeout_without_commits)
    second = _run(session, task)
    run2 = session.get(Run, second["run_id"])

    assert second["status"] == "timeout" and second.get("salvaged") is True, second
    evidence = artifact_service.handoff_delivery_evidence(run2.id)
    assert evidence["baseline_sha"] == run1.commit_sha
    assert evidence["run_commits"] == [] and evidence["run_commit_count"] == 0
    assert evidence["prior_run_commit_count"] == 1
    assert evidence["claim_status"] == {"MOCK_CHANGE.md": "prior_run_commit"}
    assert evidence["claims_all_backed_by_this_run_commits"] is False
    text = artifact_service.render_delivery_evidence_md(evidence, "salvaged")
    assert "MOCK_CHANGE.md -> prior_run_commit" in text
    assert "claims_all_backed_by_this_run_commits=false" in text
    assert "earlier commits on this branch, NOT created by this run (1)" in text
    events = [e for e in _events(session, task, "handoff_salvaged") if e.run_id == run2.id]
    assert len(events) == 1, [e.details for e in events]
    payload = structured_log.parse(events[0].details)
    assert payload["claims_all_backed_by_this_run_commits"] is False
    assert payload["prior_run_commit_count"] == 1


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
