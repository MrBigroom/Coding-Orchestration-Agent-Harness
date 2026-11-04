"""Recovery commits are machine-identifiable, and the two dirtiness predicates are separate.

Offline and deterministic: a real local git repo in tmp, no network, no external harness.
"""

from __future__ import annotations

import pytest

from orchestrator.core.models import Task
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import git_service, log_service, project_service, task_service
from orchestrator.utils import structured_log
from orchestrator.utils.git import GitError, run_git
from tests.conftest import make_repo, requires_git

pytestmark = requires_git


def _commit(path, *args: str) -> None:
    run_git(["-c", "user.name=w", "-c", "user.email=w@w", *args], path)


def _dirty_worktree(home, tmp_path, name="repo", task_id=11, branch="phase-1-r"):
    repo = make_repo(
        tmp_path / name,
        {"README.md": "# test\n", "docs/frozen-contracts.md": "# frozen\n", "app.py": "x = 1\n"},
    )
    wt = git_service.ensure_worktree(repo, name, task_id, branch, "main")
    return repo, wt


def test_recovery_commit_keeps_subject_prefix_and_adds_trailer(home, tmp_path):
    repo, wt = _dirty_worktree(home, tmp_path)
    (wt.path / "app.py").write_text("x = undefined_name\n")  # half-finished edit
    (wt.path / "scratch").mkdir()
    (wt.path / "scratch" / "notes.md").write_text("wip\n")

    info = git_service.recovery_commit_info(
        wt.path, "uncommitted worker output from R-1", run_short_id="R-1"
    )
    assert info is not None
    subject = run_git(["log", "-1", "--format=%s"], wt.path).stdout.strip()
    # The historical prefix is preserved byte-for-byte (tests, docs and GUIs match on it).
    assert subject == "chore(orchestrator-recovery): uncommitted worker output from R-1"

    body = run_git(["log", "-1", "--format=%B"], wt.path).stdout
    assert git_service.recovery_run_of(body) == "R-1", body
    # tracked vs untracked are stated separately, inside the commit itself
    assert "Tracked modifications (1): app.py" in body
    assert "Newly added untracked paths (1): scratch/notes.md" in body

    assert info.tracked_modified == ["app.py"]
    assert info.untracked_added == ["scratch/notes.md"]
    assert info.trailer == "Orchestrator-Recovery: R-1"


def test_recovery_detected_by_trailer_not_by_subject(home, tmp_path):
    """A Worker may copy the subject wording; only the trailer makes a commit a recovery commit."""
    repo, wt = _dirty_worktree(home, tmp_path, task_id=12, branch="phase-1-t")
    (wt.path / "a.txt").write_text("a\n")
    assert git_service.recovery_commit_info(wt.path, "real work", run_short_id="R-2") is not None

    # A hand-authored commit that impersonates the subject, with no trailer.
    (wt.path / "b.txt").write_text("b\n")
    _commit(wt.path, "add", "b.txt")
    _commit(
        wt.path,
        "commit",
        "-q",
        "-m",
        "chore(orchestrator-recovery): looks like wreckage but is not",
    )

    commits = git_service.commits_against_base(wt.path, "main")
    assert [c.recovery for c in commits] == [True, False]
    assert commits[0].recovery_run == "R-2"
    assert commits[1].recovery_run is None
    assert [c.short for c in git_service.recovery_commits(wt.path, "main")] == [commits[0].short]
    # ... and the branch listing is read from the repo when the worktree is gone
    from_repo = git_service.recovery_commits(repo, "main", ref="phase-1-t")
    assert [c.short for c in from_repo] == [commits[0].short]


def test_sensitive_files_stay_out_of_recovery_commit(home, tmp_path):
    repo, wt = _dirty_worktree(home, tmp_path, task_id=13, branch="phase-1-s")
    (wt.path / ".env").write_text("API_KEY=nope\n")
    (wt.path / "code.py").write_text("x = 1\n")
    info = git_service.recovery_commit_info(wt.path, "leftovers", run_short_id="R-3")
    assert info is not None
    assert info.skipped_sensitive == [".env"]
    files = run_git(["show", "--name-only", "--format=", "HEAD"], wt.path).stdout.split()
    assert files == ["code.py"]
    assert (wt.path / ".env").exists()  # left untracked, never committed


def test_recovery_event_details_are_machine_readable(home, tmp_path):
    repo, wt = _dirty_worktree(home, tmp_path, task_id=14, branch="phase-1-e")
    (wt.path / "app.py").write_text("y = 2\n")
    (wt.path / "out.log").write_text("noise\n")
    info = git_service.recovery_commit_info(wt.path, "sweep", run_short_id="R-4")
    assert info is not None
    details = git_service.recovery_event_details("worker left uncommitted changes", info)
    assert details.startswith("worker left uncommitted changes")
    payload = git_service.parse_recovery_event_details(details)
    assert payload is not None
    assert payload["recovery"] is True
    assert payload["run_short_id"] == "R-4"
    assert payload["sha"] == info.sha
    assert payload["tracked_modified"] == ["app.py"]
    assert payload["untracked_added"] == ["out.log"]
    # legacy/plain details stay readable (additive change, old lines still parse to None)
    assert git_service.parse_recovery_event_details("dirty worktree before retry") is None


def test_structured_log_roundtrip_and_absence():
    line = structured_log.attach("summary", {"a": 1})
    assert structured_log.parse(line) == {"a": 1}
    assert structured_log.summary_of(line) == "summary"
    assert structured_log.parse("no payload here") is None
    assert structured_log.parse("broken | findings_json={") is None


def test_dirty_predicates_separate_tracked_from_untracked(home, tmp_path):
    repo, wt = _dirty_worktree(home, tmp_path, task_id=15, branch="phase-1-d")
    assert not git_service.is_dirty(wt.path) and not git_service.tracked_dirty(wt.path)

    (wt.path / "node_modules_local").mkdir()
    (wt.path / "node_modules_local" / "x.js").write_text("// scratch\n")
    split = git_service.status_split(wt.path)
    assert split.tracked_modified == []
    assert split.untracked_added and split.untracked_added[0].startswith("node_modules_local")
    # untracked scratch: dirty for *recovery/cleanup*, clean for *merge/doctor*
    assert split.dirty and git_service.is_dirty(wt.path)
    assert not split.tracked_dirty and not git_service.tracked_dirty(wt.path)

    (wt.path / "app.py").write_text("z = 3\n")
    split = git_service.status_split(wt.path)
    assert sorted(split.tracked_modified) == ["app.py"]
    assert split.dirty and split.tracked_dirty and git_service.tracked_dirty(wt.path)


def test_cleanup_still_refuses_untracked_output(home, tmp_path):
    """Cleanup must not discard anything the Worker left, tracked or not (DoD 9 keeps the
    inclusive predicate exactly where losing a file would be the damage)."""
    repo, wt = _dirty_worktree(home, tmp_path, task_id=16, branch="phase-1-c")
    (wt.path / "untracked-worker-output.txt").write_text("do not delete me\n")
    infos = git_service.managed_worktrees()
    assert len(infos) == 1
    assert "has uncommitted changes" in git_service.remove_worktree(repo, infos[0], dry_run=False)
    assert wt.path.exists() and (wt.path / "untracked-worker-output.txt").exists()


def test_unauthorized_contract_change_is_computed_not_asserted(session, initialized, tmp_path):
    repo = make_repo(
        tmp_path / "proj",
        {"README.md": "# test\n", "docs/frozen-contracts.md": "# frozen\n", "docs/plan.md": "# p\n"},
    )
    project = project_service.create(session, ProjectCreate(name="proj", path=str(repo)))
    wt = git_service.ensure_worktree(repo, project.slug, 1, "phase-1-x", "main")
    (wt.path / "docs" / "frozen-contracts.md").write_text("# frozen\n\n- quietly widened\n")

    task = task_service.create(
        session,
        TaskCreate(
            title="no contract business",
            project_id=project.id,
            phase_number=1,
            phase_slug="x",
            definition_of_done=["README.md updated"],
        ),
    )
    findings = project_service.contract_findings(task, project, wt.path)
    assert findings["unauthorized"] == ["docs/frozen-contracts.md"]
    assert findings["changed"] == ["docs/frozen-contracts.md"]
    assert findings["authorized"] == []

    # The same task, with a DoD item that GRANTS the edit, authorizes it. A mention would not.
    authorized = task_service.create(
        session,
        TaskCreate(
            title="widen contracts",
            project_id=project.id,
            phase_number=2,
            phase_slug="x2",
            definition_of_done=[
                "authorized to amend docs/frozen-contracts.md to add the new event name"
            ],
        ),
    )
    again = project_service.contract_findings(authorized, project, wt.path)
    assert again["unauthorized"] == [] and again["authorized"] == ["docs/frozen-contracts.md"]


def _auth_scene(session, tmp_path, name: str = "auth"):
    """The same scene for all four ruling-1 proofs: a worktree whose
    `docs/frozen-contracts.md` has been edited with no authorization anywhere.

    Keeping the scene identical means a difference in verdict can only come from the task's
    stored fields, which is exactly the thing under test.
    """
    repo = make_repo(
        tmp_path / name,
        {"README.md": "# test\n", "docs/frozen-contracts.md": "# frozen\n"},
    )
    project = project_service.create(session, ProjectCreate(name=name, path=str(repo)))
    wt = git_service.ensure_worktree(repo, project.slug, 1, f"phase-1-{name}", "main")
    (wt.path / "docs" / "frozen-contracts.md").write_text("# frozen\n\n- widened\n")
    return repo, project, wt


def _shaped(session, project, **fields) -> Task:
    return task_service.create(
        session,
        TaskCreate(
            title="authorization shapes",
            project_id=project.id,
            phase_number=1,
            phase_slug="auth",
            **fields,
        ),
    )


def _verdict(session, project, task, changed: list[str]) -> tuple[set[str], list[str]]:
    return (
        project_service.authorized_contract_files(task),
        project_service.unauthorized_contract_changes(task, changed),
    )


CONTRACT_CHANGED = ["docs/frozen-contracts.md"]


def test_contract_authorization_a_prohibition_is_not_a_grant(session, initialized, tmp_path):
    """Ruling 1 case (a): a prohibition naming the file still yields the unauthorized finding.

    This was the fail-open shape: T-8's own brief says "Do NOT modify
    docs/frozen-contracts.md", and a mention-based matcher read that as authorization for the
    very edit it exists to catch.
    """
    _repo, project, _wt = _auth_scene(session, tmp_path, "auth-a")

    task = _shaped(
        session,
        project,
        definition_of_done=["Do NOT modify docs/frozen-contracts.md"],
        acceptance_criteria=["docs/frozen-contracts.md must not be edited by this phase"],
    )
    assert _verdict(session, project, task, CONTRACT_CHANGED) == (set(), CONTRACT_CHANGED)

    # a negated grant is still a prohibition, whichever negation wording it uses
    for wording in (
        "not authorized to change docs/frozen-contracts.md",
        "never edit docs/frozen-contracts.md",
        "out of scope: docs/frozen-contracts.md",
        "docs/frozen-contracts.md is forbidden",
        "this phase may not amend docs/frozen-contracts.md",
    ):
        task = _shaped(session, project, definition_of_done=[wording])
        assert _verdict(session, project, task, CONTRACT_CHANGED) == (set(), CONTRACT_CHANGED), wording

    # and T-8's own stored authorization is exactly this shape: it names all five files to
    # describe the detector, and forbids the edit — so T-8 must not authorize itself
    task = _shaped(
        session,
        project,
        definition_of_done=[
            "the changed-file set is intersected with the contract file list the harness already "
            "knows (docs/plan.md, docs/product-spec.md, docs/architecture.md, docs/workflow.md, "
            "docs/frozen-contracts.md)"
        ],
        frozen_contracts=["Do NOT modify docs/frozen-contracts.md"],
    )
    assert _verdict(session, project, task, CONTRACT_CHANGED) == (set(), CONTRACT_CHANGED)


def test_contract_authorization_an_explicit_grant_suppresses_the_finding(session, initialized, tmp_path):
    """Ruling 1 case (b): an explicit grant in a stored authorization field suppresses it."""
    _repo, project, wt = _auth_scene(session, tmp_path, "auth-b")

    task = _shaped(
        session,
        project,
        definition_of_done=[
            "authorized to amend docs/frozen-contracts.md to add the ORCH_VERIFY field names"
        ],
    )
    assert _verdict(session, project, task, CONTRACT_CHANGED) == ({"docs/frozen-contracts.md"}, [])
    # the finding the gate would raise is really gone, not just relabelled
    assert project_service.contract_findings(task, project, wt.path) == {
        "changed": CONTRACT_CHANGED,
        "authorized": CONTRACT_CHANGED,
        "unauthorized": [],
    }

    # a grant in any of the three stored fields counts; the verb may be phrased several ways
    assert _verdict(
        session,
        project,
        _shaped(
            session,
            project,
            acceptance_criteria=["docs/frozen-contracts.md may be extended with the new event"],
        ),
        CONTRACT_CHANGED,
    ) == ({"docs/frozen-contracts.md"}, [])
    assert _verdict(
        session,
        project,
        _shaped(session, project, frozen_contracts=["this phase is permitted to update docs/plan.md"]),
        ["docs/plan.md"],
    ) == ({"docs/plan.md"}, [])

    # a grant for file X does not leak to file Y
    task = _shaped(
        session,
        project,
        definition_of_done=["authorized to amend docs/plan.md to add the new section"],
    )
    assert _verdict(session, project, task, CONTRACT_CHANGED) == ({"docs/plan.md"}, CONTRACT_CHANGED)


def test_contract_authorization_commit_message_and_diffstat_grant_nothing(session, initialized, tmp_path):
    """Ruling 1 case (c): paths in a commit message, a diffstat or the description authorize zero."""
    _repo, project, wt = _auth_scene(session, tmp_path, "auth-c")

    # nothing stored at all -> not authorized
    bare = _shaped(session, project, definition_of_done=["README.md updated"])
    assert _verdict(session, project, bare, CONTRACT_CHANGED) == (set(), CONTRACT_CHANGED)

    # the task DESCRIPTION talks about amending the file freely; it is not an authorization source
    described = _shaped(
        session, project, description="amend docs/frozen-contracts.md freely, it is authorized"
    )
    assert project_service.authorized_contract_files(described) == set()

    # a COMMIT MESSAGE that claims a grant, plus the DIFFSTAT that names the file
    run_git(
        [
            "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-m",
            "authorized to amend docs/frozen-contracts.md: widening the event list", "-a",
        ],
        wt.path,
    )
    assert "docs/frozen-contracts.md" in run_git(["log", "--format=%B"], wt.path).stdout
    assert "frozen-contracts.md" in run_git(["diff", "--stat", "main"], wt.path).stdout
    findings = project_service.contract_findings(bare, project, wt.path)
    assert findings["unauthorized"] == CONTRACT_CHANGED  # committed words grant nothing
    assert findings["authorized"] == []


def test_contract_authorization_a_bare_mention_grants_nothing(session, initialized, tmp_path):
    """Ruling 1, the mention half: naming the file in prose is not permission to change it."""
    _repo, project, _wt = _auth_scene(session, tmp_path, "auth-d")
    task = _shaped(
        session,
        project,
        definition_of_done=[
            "report which of docs/frozen-contracts.md, docs/plan.md were touched by the run"
        ],
    )
    assert project_service.authorized_contract_files(task) == set()
    assert project_service.unauthorized_contract_changes(task, CONTRACT_CHANGED) == CONTRACT_CHANGED


def test_reviewer_prompt_sections_are_rendered_from_their_builders(session, initialized, tmp_path):
    """The recovery + harness-findings sections reach the Architect prompt.

    T-11 now archives the full payload (PROPOSED layout addition, see
    docs/contract-change-request-T11-review-payload.md); this test stays at the section builders
    so it pins the text itself rather than the archive wrapper.
    """
    from orchestrator.services import prompt_builder

    state = prompt_builder.DynamicState(
        recovery="- abc1234 (recovery of run R-9): chore(orchestrator-recovery): sweep",
        harness_findings="recovery commits on this branch (harness-created, never self-checked): 1",
    )
    rendered = prompt_builder._dynamic(state)
    assert "Recovery commits on this branch" in rendered
    assert "NEVER self-checked" in rendered
    assert "Harness-detected findings" in rendered
    assert "abc1234" in rendered
    # an empty state adds nothing: the sections are opt-in, not always-on noise
    assert "Recovery commits" not in prompt_builder._dynamic(prompt_builder.DynamicState())


def test_doctor_reports_untracked_dirt_without_failing_cleanliness(session, initialized, tmp_path):
    repo = make_repo(tmp_path / "proj", {"README.md": "# test\n"})
    project = project_service.create(session, ProjectCreate(name="proj", path=str(repo)))
    (repo / "tool-state").mkdir()
    (repo / "tool-state" / "cache.bin").write_text("junk")

    checks = {c["name"]: c for c in project_service.doctor(project)}
    assert checks["main checkout clean"]["ok"] is True
    assert checks["untracked paths in the base checkout"]["ok"] is True
    assert "tool-state/cache.bin" in checks["untracked paths in the base checkout"]["detail"]

    (repo / "README.md").write_text("uncommitted tracked change\n")
    checks = {c["name"]: c for c in project_service.doctor(project)}
    assert checks["main checkout clean"]["ok"] is False
    assert "README.md" in checks["main checkout clean"]["detail"]
    (repo / "README.md").write_text("# test\n")


def test_new_round_resets_contradictory_lifecycle_fields(session, initialized, tmp_path):
    """A task header cannot report `in_progress` while still carrying the previous round's
    `RUN_TIMEOUT` marker (DoD 7) — and a requeue must NOT throw away the verdict the Worker
    is about to act on."""
    repo = make_repo(tmp_path / "proj", {"README.md": "# test\n"})
    project = project_service.create(session, ProjectCreate(name="proj", path=str(repo)))
    task = task_service.create(
        session,
        TaskCreate(title="retry me", project_id=project.id, phase_number=1, phase_slug="x"),
    )
    task_service.transition(session, task, "queued")
    task_service.transition(session, task, "in_progress")
    # what a rejected, then timed-out round leaves behind
    task.handoff_status = "RUN_TIMEOUT"
    task.review_status = "CHANGES_REQUESTED"
    task.verification_status = "fail"
    task.merge_status = "stale"
    session.add(task)
    session.commit()

    task_service.transition(session, task, "planned", "run timeout")
    task_service.queue(session, task)
    assert task.status == "queued"
    assert task.handoff_status == "RUN_TIMEOUT"  # still true: no new round has started
    assert task.review_status == "CHANGES_REQUESTED"  # feedback the Worker must see

    cleared = task_service.clear_round_fields(session, task, reason="run R-2 started")
    assert cleared == ["handoff_status", "review_status", "verification_status", "merge_status"]
    assert task.handoff_status is None
    assert task.review_status is None and task.verification_status is None
    assert task.merge_status is None
    events = [(e.event, e.details) for e in log_service.recent(session, limit=50)]
    assert ("retry_fields_reset", "cleared handoff_status, review_status, verification_status, "
            "merge_status: run R-2 started") in events, events
    # clearing twice in a row logs nothing new (no empty churn in the build log)
    assert task_service.clear_round_fields(session, task, reason="again") == []


def test_merge_refusal_for_tracked_dirt_and_recovery_still_captures_untracked(home, tmp_path):
    repo, wt = _dirty_worktree(home, tmp_path, task_id=17, branch="phase-1-m")
    (wt.path / "untracked.txt").write_text("worker output\n")
    (wt.path / "README.md").write_text("tracked edit\n")
    _commit(wt.path, "add", "-A")
    _commit(wt.path, "commit", "-q", "-m", "worker commit")

    # The base checkout has TRACKED dirt -> refusal.
    (repo / "README.md").write_text("oops\n")
    with pytest.raises(GitError, match="uncommitted changes to tracked"):
        git_service.merge_no_ff(repo, "main", "phase-1-m", "msg")
    # Recovery still sweeps the untracked half in a worktree that is untracked-only dirty.
    _commit(repo, "checkout", "-q", "--", "README.md")
    (wt.path / "more-scratch.bin").write_bytes(b"\x00\x01")
    assert not git_service.tracked_dirty(wt.path) and git_service.is_dirty(wt.path)
    info = git_service.recovery_commit_info(wt.path, "untracked only", run_short_id="R-9")
    assert info is not None and info.untracked_added == ["more-scratch.bin"]
    assert info.tracked_modified == []
