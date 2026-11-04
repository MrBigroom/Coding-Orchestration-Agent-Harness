"""The pure half of T-10: reading a worktree handoff, and classifying its claims against git.

Offline and deterministic: a real local git repo in tmp, no network, no live harness, no provider.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.core.schemas import Handoff
from orchestrator.services import artifact_service, git_service, run_service
from orchestrator.utils.git import run_git
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

VALID = {
    "phase": "phase-1-p1",
    "branch": "phase-1-p1",
    "status": "READY_FOR_REVIEW",
    "summary": "did the thing",
    "files_changed": ["app.py"],
}


def _commit(path: Path, message: str, *files: str) -> None:
    run_git(["add", *files], path)
    run_git(["-c", "user.name=w", "-c", "user.email=w@w", "commit", "-q", "-m", message], path)


def _repo(tmp_path) -> Path:
    return make_repo(tmp_path / "repo", {"README.md": "# test\n", "app.py": "x = 1\n"})


# ---------------------------------------------------------------- read_handoff_file


def test_read_handoff_file_absent_is_not_an_error(tmp_path):
    handoff, error = run_service.read_handoff_file(tmp_path / "handoff.json")
    assert handoff is None and error is None  # nothing to salvage, nothing to log


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "is empty"),
        ("   \n", "is empty"),
        ('{"phase": "p", "summary": "trunca', "not valid JSON"),
        ('{"phase": "phase-1-p1"}', "does not validate as Handoff"),
        ('{"status": "ALMOST_READY", "phase": "p", "branch": "b", "summary": "s"}', "status"),
    ],
)
def test_read_handoff_file_reports_why_it_cannot_use_the_file(tmp_path, raw, fragment):
    path = tmp_path / "handoff.json"
    path.write_text(raw)
    handoff, error = run_service.read_handoff_file(path)
    assert handoff is None
    assert error and fragment in error, error


def test_read_handoff_file_accepts_a_valid_submission(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(VALID, indent=2))
    handoff, error = run_service.read_handoff_file(path)
    assert error is None
    assert isinstance(handoff, Handoff) and handoff.status == "READY_FOR_REVIEW"


def test_exit_state_text_names_the_run_and_the_reason():
    class _Run:
        short_id = "R-1"
        status = "timeout"

    result = run_service.AdapterResult(exit_code=None, timed_out=True, error="timed out after 2700s")
    text = run_service.exit_state_text(_Run(), result)  # type: ignore[arg-type]
    assert "run R-1 status=timeout" in text
    assert "timed_out=True" in text
    assert "adapter timeout killed the process" in text
    assert "exit code -1" not in text


# ---------------------------------------------------------------- delivery_evidence


def _evidence(wt: Path, claims: list[str], base: str = "main") -> dict:
    handoff = Handoff.model_validate({**VALID, "files_changed": claims})
    return run_service.delivery_evidence(
        wt, base, handoff, arrival="salvaged", exit_state="run R-1 status=timeout"
    )


def test_claims_are_classified_against_commits_dirt_and_absence(home, tmp_path):
    repo = _repo(tmp_path)
    wt = git_service.ensure_worktree(repo, "repo", 1, "phase-1-p1", "main").path
    # a Worker commit
    (wt / "worker.py").write_text("print('hi')\n")
    _commit(wt, "feat: worker change", "worker.py")
    # wreckage the harness committed, never self-checked by a Worker
    (wt / "wreckage.py").write_text("x = undefined_name\n")
    info = git_service.recovery_commit_info(wt, "uncommitted worker output from R-1", "R-1")
    assert info and info.sha
    # dirt that is still only a promise
    (wt / "promise.md").write_text("claimed, not committed\n")
    (wt / "notes").mkdir()
    (wt / "notes" / "scratch.txt").write_text("noise\n")

    evidence = _evidence(
        wt, ["worker.py", "wreckage.py", "promise.md", "notes/scratch.txt", "never-existed.md"]
    )
    assert evidence["claim_status"] == {
        "worker.py": artifact_service.CLAIM_WORKER_COMMIT,
        "wreckage.py": artifact_service.CLAIM_RECOVERY_COMMIT,
        "promise.md": artifact_service.CLAIM_UNCOMMITTED,
        "notes/scratch.txt": artifact_service.CLAIM_UNCOMMITTED,
        "never-existed.md": artifact_service.CLAIM_ABSENT,
    }
    assert evidence["claims_all_backed_by_worker_commits"] is False
    assert evidence["worker_commit_count"] == 1 and evidence["recovery_commit_count"] == 1
    assert evidence["commit_count"] == 2
    assert evidence["claim_counts"] == {
        "worker_commit": 1,
        "recovery_commit_only": 1,
        "uncommitted_only": 2,
        "absent": 1,
    }
    # the raw evidence a reviewer needs to check the classification, not just its conclusion
    assert "promise.md" in " ".join(evidence["porcelain_at_ingestion"])
    assert any(line.startswith("??") for line in evidence["porcelain_at_ingestion"])
    assert "worker.py" in evidence["diffstat"]
    assert evidence["commits"][1]["recovery"] is True and evidence["commits"][0]["recovery"] is False
    assert evidence["arrival"] == "salvaged" and evidence["exit_state"].startswith("run R-1")


def test_a_path_only_a_worker_commit_touched_still_counts_when_the_harness_sweeps_more(
    home, tmp_path
):
    repo = _repo(tmp_path)
    wt = git_service.ensure_worktree(repo, "repo", 2, "phase-1-p1", "main").path
    (wt / "README.md").write_text("# edited by the Worker\n")
    _commit(wt, "docs: mine", "README.md")
    (wt / "scratch.txt").write_text("leftover\n")
    assert git_service.recovery_commit_info(wt, "uncommitted worker output from R-2", "R-2")

    evidence = _evidence(wt, ["README.md"])
    assert evidence["claim_status"] == {"README.md": artifact_service.CLAIM_WORKER_COMMIT}
    assert evidence["claims_all_backed_by_worker_commits"] is True


def test_normalised_prefixes_do_not_hide_a_claim(home, tmp_path):
    repo = _repo(tmp_path)
    wt = git_service.ensure_worktree(repo, "repo", 3, "phase-1-p1", "main").path
    (wt / "app.py").write_text("x = 2\n")
    _commit(wt, "fix: app", "app.py")
    assert _evidence(wt, ["./app.py", " /app.py"])["claims_all_backed_by_worker_commits"] is True


def test_a_handoff_with_no_claims_is_not_reported_as_delivered(home, tmp_path):
    repo = _repo(tmp_path)
    wt = git_service.ensure_worktree(repo, "repo", 4, "phase-1-p1", "main").path
    evidence = _evidence(wt, [])
    assert evidence["claimed_count"] == 0
    assert evidence["claims_all_backed_by_worker_commits"] is False


def test_no_handoff_at_all_yields_an_empty_claim_set(home, tmp_path):
    repo = _repo(tmp_path)
    wt = git_service.ensure_worktree(repo, "repo", 5, "phase-1-p1", "main").path
    evidence = run_service.delivery_evidence(
        wt, "main", None, arrival="synthesized", exit_state="n/a"
    )
    assert evidence["claimed_count"] == 0 and evidence["claim_status"] == {}
    assert evidence["commits"] == []


# ---------------------------------------------------------------- renderer


def test_render_is_empty_when_there_is_nothing_to_render():
    assert artifact_service.render_delivery_evidence_md({}, "salvaged") == ""
    assert artifact_service.render_delivery_evidence_md({"commit_count": 0}, None) != ""


def test_render_carries_the_provenance_and_the_flag(home, tmp_path):
    repo = _repo(tmp_path)
    wt = git_service.ensure_worktree(repo, "repo", 6, "phase-1-p1", "main").path
    (wt / "promise.md").write_text("claimed, not committed\n")
    evidence = _evidence(wt, ["promise.md"])
    evidence["uncommitted_before_harness_sweep"] = ["promise.md"]
    text = artifact_service.render_delivery_evidence_md(evidence, "salvaged")
    assert "handoff arrival: salvaged" in text
    assert "run exit state: run R-1 status=timeout" in text
    assert "promise.md -> uncommitted_only" in text
    assert "claims_all_backed_by_worker_commits=false" in text
    assert "uncommitted when the run ended (before the harness recovery sweep): promise.md" in text
    assert "PROMISES, not delivered work" in text
    assert "commits on this branch since main: 0" in text


# ------------------------------------------- mock harness plumbing (why 9 tests could lie green)


def test_mock_child_process_is_pinned_to_the_checkout_under_test(tmp_path):
    """The mock's own subprocess must import THIS checkout.

    `core.security.BASE_ENV_ALLOWLIST` deliberately excludes PYTHONPATH, and this repo's venv is
    an editable install pointing at the main checkout, so `python -m
    orchestrator.adapters.mock_harness` in a worktree used to execute the MAIN copy of the file.
    That is how every T-10 integration test asked for a timeout and got a `completed` run: the
    stale copy did not know the new tags and fell through to the success path.
    """
    from orchestrator.adapters import mock_harness
    from orchestrator.adapters.base import AdapterResult

    adapter = mock_harness.MockHarnessAdapter(mock_harness.MockHarnessAdapter.default_spec())
    ctx = mock_harness.RunContext(
        run_id=1,
        task_id=1,
        cwd=tmp_path,
        prompt_file=tmp_path / "prompt.md",
        artifacts_dir=tmp_path,
        handoff_path=tmp_path / "handoff.json",
        branch="phase-1-p1",
        phase="p1",
        tags=["mock-timeout-handoff"],
        timeout_seconds=600,
    )
    seen: dict = {}

    async def fake_runner(**kw):
        seen.update(kw)
        return AdapterResult(exit_code=0)

    result = asyncio.run(adapter.run(ctx, fake_runner))

    assert result.exit_code == 0
    assert seen["extra_env"]["PYTHONPATH"] == str(mock_harness.PACKAGE_SRC)
    # the pinned root really is the tree holding this module, not the main checkout
    assert (mock_harness.PACKAGE_SRC / "orchestrator" / "adapters" / "mock_harness.py").is_file()
    assert ctx.timeout_seconds == mock_harness.MOCK_TIMEOUT_SECONDS


def test_mock_harness_refuses_an_unknown_behaviour_tag(tmp_path, capsys):
    """A tag the mock does not understand must fail the run, never look like success."""
    from orchestrator.adapters import mock_harness

    prompt = tmp_path / "prompt.md"
    prompt.write_text("x\n")
    handoff = tmp_path / "handoff.json"
    rc = mock_harness.main(
        [
            "--cwd",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--handoff",
            str(handoff),
            "--branch",
            "phase-1-p1",
            "--run-id",
            "1",
            "--tag",
            "mock-timeout-handof",  # typo: the shape that silently completed a run
        ]
    )
    assert rc == 2
    assert not handoff.exists()
    assert "unknown behaviour tag" in capsys.readouterr().err
