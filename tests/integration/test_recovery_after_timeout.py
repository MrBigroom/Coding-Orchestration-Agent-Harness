"""T-8 end to end: a timeout leaves half-finished edits; the harness commits them *as
recovery*, tells the next run about them, and deterministic verification — not the Worker's
story — decides what happened.

Offline: mock harness + mock provider against a real local git repo. No network, no services.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.core.models import Run, TaskStatus
from orchestrator.core.schemas import ProjectCreate, TaskCreate
from orchestrator.services import (
    artifact_service,
    git_service,
    log_service,
    project_service,
    review_service,
    run_service,
    settings_service,
    supervisor_service,
    task_service,
)
from orchestrator.utils import structured_log
from orchestrator.utils.git import run_git
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

# The project's own suite imports the source tree, so a half-finished file on the branch makes
# verification fail on its own merits — the harness never guesses about intent.
RUN_TESTS = """import importlib, sys
try:
    mod = importlib.import_module("app")
    mod.handle()
except ModuleNotFoundError:
    pass
except Exception as exc:
    print(f"FAILED: app.handle() raised {exc!r}")
    sys.exit(1)
print("3 passed, 0 failed")
"""
BROKEN_APP = "def handle():\n    return not_yet_defined(1)\n"

# A stdlib-only linter with the shape of ruff's F821, so DoD 5's "a lint error such as an
# undefined name" is surfaced by a LINT command and not only by the test run. Offline, no
# dependencies: the run-5 wreckage (`GitError` used at project_service.py:112 but imported only
# at l.226) is exactly this defect class, and it survived because nothing re-verified the branch.
LINT_NAMES = '''"""Report names that are used but never defined (undefined-name lint, stdlib only)."""
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
def client(initialized):
    from orchestrator.api.main import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c


def _project(session, tmp_path, name="proj"):
    repo = make_repo(
        tmp_path / name,
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
            name=name,
            path=str(repo),
            test_command="python3 run_tests.py",
            lint_command="python3 lint_names.py",
        ),
    )


def _task(session, project, tags, phase, dod=("README.md updated",), title="phase"):
    return task_service.create(
        session,
        TaskCreate(
            title=title,
            project_id=project.id,
            tags=list(tags),
            phase_number=phase,
            phase_slug=f"p{phase}",
            definition_of_done=list(dod),
        ),
    )


def _run(session, task):
    outcome = asyncio.run(run_service.run_task_now(session, task))
    session.refresh(task)
    return outcome


def _next_round(session, task, tags, phase):
    task = task_service.get(session, task.id)
    task.tags = list(tags)
    task.phase_number = phase
    task.phase_slug = f"p{phase}"
    session.add(task)
    session.commit()
    session.refresh(task)
    return _run(session, task)


def _events(session, task, event):
    return [e for e in log_service.recent(session, task_id=task.id, limit=300) if e.event == event]


def _header_spy(session, task, monkeypatch) -> dict:
    """Snapshot the task header at the moment the pre-run recovery sweep runs — i.e. while the
    new round is live and `in_progress`.

    Two layers are captured, because DoD 7 is about what a HUMAN reads: the ORM row, and the
    exact payload the GUI header renders (`routes/tasks.py:_detail`, which `TaskDetail.tsx`
    feeds into `<Badge value={task.status} />` and `<Badge value={task.handoff_status} />`)."""
    from orchestrator.api.routes import tasks as tasks_routes

    seen: dict[str, object] = {}
    original = git_service.recovery_commit_info

    def spy(path, label, run_short_id=None):
        live = task_service.get(session, task.id)
        seen.update(
            status=live.status,
            handoff_status=live.handoff_status,
            review_status=live.review_status,
            verification_status=live.verification_status,
            merge_status=live.merge_status,
        )
        payload = tasks_routes._detail(session, live)
        seen.update(
            header_status=payload["status"],
            header_handoff_status=payload["handoff_status"],
            header_review_status=payload["review_status"],
        )
        return original(path, label, run_short_id=run_short_id)

    monkeypatch.setattr(git_service, "recovery_commit_info", spy)
    return seen


def test_timeout_wreckage_is_committed_as_recovery_and_told_to_the_next_run(
    session, monkeypatch, tmp_path
):
    project = _project(session, tmp_path)
    task = _task(session, project, ["mock-timeout"], 1)

    # ---- round 1: the run times out. Nothing was ever self-checked. -----------------
    outcome = _run(session, task)
    run1 = session.get(Run, outcome["run_id"])
    assert outcome["status"] == "timeout" and run1.status == "timeout"
    assert task.status == TaskStatus.planned
    assert task.handoff_status == "RUN_TIMEOUT"
    assert _events(session, task, "recovery_commit") == []  # a clean timeout sweeps nothing

    wt = Path(run1.worktree_path)
    assert wt.is_dir() and git_service.current_branch(wt) == task.branch_name

    # ---- what a killed Worker actually leaves in that worktree ---------------------
    (wt / "app.py").write_text(BROKEN_APP)  # new, untracked, half-finished
    (wt / "README.md").write_text("x\n# WIP, do not trust this\n")  # tracked modification
    (wt / "notes").mkdir()
    (wt / "notes" / "scratch.txt").write_text("thinking out loud\n")

    seen = _header_spy(session, task, monkeypatch)

    # ---- round 2: the sweep commits it, labels it, and says so in the prompt -------
    outcome = _next_round(session, task, ["mock"], 2)
    run2 = session.get(Run, outcome["run_id"])

    # the reported defect: `in_progress` carrying the previous round's `RUN_TIMEOUT` marker
    assert seen["status"] == TaskStatus.in_progress, seen
    assert seen["handoff_status"] is None, seen
    assert seen["review_status"] is None and seen["verification_status"] is None, seen
    # ... and the GUI header is built from that same payload, so it cannot show both
    assert seen["header_status"] == "in_progress", seen
    assert seen["header_handoff_status"] is None, seen
    assert seen["header_review_status"] is None, seen
    assert len(_events(session, task, "retry_fields_reset")) == 1

    swept = _events(session, task, "recovery_commit")
    assert len(swept) == 1, [e.details for e in swept]
    assert swept[0].event == "recovery_commit" and swept[0].commit_sha

    payload = git_service.parse_recovery_event_details(swept[0].details)
    assert payload is not None, swept[0].details  # machine-readable, as DoD 2 requires
    assert payload["recovery"] is True
    assert payload["run_short_id"] == run1.short_id  # names the aborted run, not the finder
    assert sorted(payload["tracked_modified"]) == ["README.md"]
    assert sorted(payload["untracked_added"]) == ["app.py", "notes/scratch.txt"]
    assert structured_log.parse(swept[0].details)["sha"] == swept[0].commit_sha

    subject = run_git(["log", "-1", "--format=%s", swept[0].commit_sha], wt).stdout.strip()
    assert subject.startswith(git_service.RECOVERY_SUBJECT_PREFIX)  # historical prefix, byte-exact
    assert "leftover changes from " in subject
    body = run_git(["log", "-1", "--format=%B", swept[0].commit_sha], wt).stdout
    assert "NEVER self-checked by the Worker" in body
    assert "Tracked modifications (1): README.md" in body
    assert "Newly added untracked paths (2): app.py, notes/scratch.txt" in body
    assert git_service.recovery_run_of(body) == run1.short_id
    assert (wt / "app.py").exists() and (wt / "notes" / "scratch.txt").exists()  # nothing lost
    assert not git_service.is_dirty(wt)

    # detection is by trailer, and it is the *only* recovery commit on the branch
    recovery = git_service.recovery_commits(wt, project.base_branch)
    assert [c.sha for c in recovery] == [swept[0].commit_sha]
    assert recovery[0].recovery_run == run1.short_id
    assert sorted(f["path"] for f in recovery[0].files) == [
        "README.md",
        "app.py",
        "notes/scratch.txt",
    ]

    prompt = artifact_service.read_text(run2.id, "prompt.md")
    assert "Recovery commits on this branch" in prompt
    assert git_service.RECOVERY_SUBJECT_PREFIX in prompt
    assert "NEVER self-checked" in prompt
    assert "re-verify the branch with the project's lint and test commands" in prompt
    assert swept[0].commit_sha[:7] in prompt  # the commit itself is named
    assert len(_events(session, task, "recovery_notice_included")) == 1

    # ---- the story says it is fine; the deterministic check says it is not ---------
    handoff = json.loads(artifact_service.read_text(run2.id, "handoff.json"))
    assert handoff["status"] == "READY_FOR_REVIEW"
    assert handoff["dod_self_check"][0]["met"] is True
    assert outcome.get("verification") == "fail", outcome
    assert "verdict" not in outcome  # it never reached the reviewer
    assert task.status == TaskStatus.queued
    assert "not_yet_defined" in artifact_service.read_text(run2.id, "test.log")

    # DoD 5: the defect class from run 5 is a LINT error (F821 undefined name), so the lint
    # command must surface it too — not only the runtime import the tests happen to do.
    lint_checks = {
        c["name"]: c
        for c in json.loads(artifact_service.read_text(run2.id, "verification.json"))["checks"]
    }
    assert lint_checks["lint"]["status"] == "fail", lint_checks["lint"]
    assert lint_checks["tests"]["status"] == "fail", lint_checks["tests"]
    lint_log = artifact_service.read_text(run2.id, "lint.log")
    assert "F821 undefined name 'not_yet_defined'" in lint_log, lint_log
    assert "app.py" in lint_log  # names the file the sweep committed, not just "something failed"

    narrative = artifact_service.read_text(run2.id, "verification.md")
    assert "changed but not claimed" in narrative  # the swept files are visible here
    assert "recovery commit" in narrative.lower()

    # round 2's own markers describe round 2, and nothing else
    assert task.handoff_status == "READY_FOR_REVIEW"
    assert task.review_status is None and task.verification_status == "fail"

    # ---- round 3 is told about it too ----------------------------------------------
    outcome = _next_round(session, task, ["mock"], 3)
    assert len(_events(session, task, "recovery_commit")) == 1  # nothing new to sweep
    prompt3 = artifact_service.read_text(outcome["run_id"], "prompt.md")
    assert "Recovery commits on this branch" in prompt3
    assert run1.short_id in prompt3
    assert "not_yet_defined" in run_git(["show", f"{recovery[0].sha}:app.py"], wt).stdout
    # and it is still failing verification, on the evidence, three rounds later
    assert outcome.get("verification") == "fail", outcome


def test_recovery_reaches_the_reviewer_and_the_gui_payload(session, client, tmp_path):
    project = _project(session, tmp_path, "reviewable")
    task = _task(session, project, ["mock-timeout"], 1, dod=("MOCK_CHANGE.md updated",))
    outcome = _run(session, task)
    run1 = session.get(Run, outcome["run_id"])
    wt = Path(run1.worktree_path)

    # Benign wreckage: untracked scratch only, so the branch stays buildable.
    (wt / "dump.log").write_text("worker noise\n")

    settings_service.update(session, {"merge_enabled": True})
    outcome = _next_round(session, task, ["mock"], 2)
    swept = _events(session, task, "recovery_commit")
    assert len(swept) == 1
    details = git_service.parse_recovery_event_details(swept[0].details)
    assert details["untracked_added"] == ["dump.log"] and details["tracked_modified"] == []
    # untracked leftovers are not "dirty" for the merge gate, and must not refuse a merge
    assert not git_service.tracked_dirty(wt)

    assert outcome.get("verification") == "pass", outcome
    assert outcome["verdict"] == "APPROVED" and task.status == TaskStatus.accepted

    text = review_service.recovery_section(session, task, session.get(Run, outcome["run_id"]))
    assert "created by the ORCHESTRATOR" in text and "NEVER self-checked" in text
    assert swept[0].commit_sha[:7] in text
    # What the reviewer is shown, asserted from the section builders rather than from an archived
    # copy of the prompt. Archiving that copy would add a file to the frozen `runs/<run_id>/`
    # artifact layout, which no DoD item grants (round 6, reviewer finding 2); review-input
    # archiving belongs to the separately tracked "truncated review input" harness item.
    run2 = session.get(Run, outcome["run_id"])
    assert "dump.log" in text  # the swept untracked path is named in the reviewer's recovery list
    findings = review_service.harness_findings_section(session, task, run2, project)
    assert "recovery commits on this branch (harness-created, never self-checked): 1" in findings
    assert "docs/frozen-contracts.md" in findings  # the watched list is stated, not implied
    assert "UNAUTHORIZED" not in findings  # nothing was touched, so nothing is flagged
    names = {a["name"] for a in artifact_service.list_artifacts(run2.id)}
    assert "review_payload.md" not in names, names

    # the GUI's own audit trail: recovery badge data, straight from the task detail response.
    # There is deliberately no GET /api/tasks/{id}/recovery (reviewer ruling 3 — dead code); the
    # detail field is the only place this is served, so the GUI has to be able to read everything
    # it renders from it.
    assert client.get(f"/api/tasks/{task.short_id}/recovery").status_code == 404
    detail = client.get(f"/api/tasks/{task.short_id}").json()
    assert [c["sha"] for c in detail["recovery_commits"]] == [swept[0].commit_sha]
    assert detail["status"] == "accepted"
    commit = detail["recovery_commits"][0]
    assert commit["recovery"] is True and commit["recovery_run"] == run1.short_id
    assert [f["path"] for f in commit["files"] if f["status"].startswith("A")] == ["dump.log"]
    assert commit["subject"].startswith(git_service.RECOVERY_SUBJECT_PREFIX)
    # what RecoveryNote / the header badge render, with nothing else to fetch
    assert sum(1 for c in detail["recovery_commits"] if c["recovery"]) > 0


def test_unauthorized_contract_edit_from_recovery_waits_for_a_human(session, tmp_path):
    project = _project(session, tmp_path, "contracts")
    task = _task(session, project, ["mock-timeout"], 1)
    outcome = _run(session, task)
    run1 = session.get(Run, outcome["run_id"])
    wt = Path(run1.worktree_path)

    # the aborted turn widened a frozen contract, and never committed it
    (wt / "docs" / "frozen-contracts.md").write_text(
        "# frozen contracts\n\n- the event names are whatever I wanted\n"
    )

    outcome = _next_round(session, task, ["mock"], 2)
    assert "verification" in outcome, outcome
    # tests are green, and that is deliberately not enough: the harness finding fails the verdict
    assert outcome["verification"] == "fail"
    checks = {c["name"]: c for c in json.loads(
        artifact_service.read_text(outcome["run_id"], "verification.json")
    )["checks"]}
    assert checks["tests"]["status"] == "pass"
    assert checks[supervisor_service.CONTRACT_CHECK]["status"] == "fail"
    assert "frozen-contracts.md" in checks[supervisor_service.CONTRACT_CHECK]["notes"]
    assert "recovery commit" in checks[supervisor_service.CONTRACT_CHECK]["notes"]
    assert checks[supervisor_service.RECOVERY_CHECK]["status"] == "pass"  # informational
    assert "NEVER self-checked" in checks[supervisor_service.RECOVERY_CHECK]["notes"]
    assert outcome["decision"] == TaskStatus.blocked
    assert outcome["contract_change"] == ["docs/frozen-contracts.md"]
    assert task.status == TaskStatus.blocked
    assert task.review_status == "NEEDS_HUMAN"
    assert "verdict" not in outcome  # the reviewer is not asked to bless a human decision

    # the question reaches the surfaces that exist: build log + blocked reason
    verdicts = _events(session, task, "verdict_recorded")
    assert len(verdicts) == 1 and "NEEDS_HUMAN" in verdicts[0].details
    assert "docs/frozen-contracts.md" in verdicts[0].details
    blocks = [e for e in _events(session, task, "task_status") if "-> blocked" in e.details]
    assert blocks and "docs/frozen-contracts.md" in blocks[0].details
    assert "authorize" in blocks[0].details

    detected = _events(session, task, "contract_change_detected")
    assert len(detected) == 1
    findings = structured_log.parse(detected[0].details)
    assert findings["unauthorized"] == ["docs/frozen-contracts.md"]
    assert findings["authorized"] == []
    assert findings["recovery_commit"] is True  # it arrived inside a recovery commit
    assert findings["recovery_commits"] == [swept_short(session, task)]

    run2 = session.get(Run, outcome["run_id"])
    text = review_service.harness_findings_section(session, task, run2, project)
    assert "UNAUTHORIZED contract file change(s)" in text
    assert "docs/frozen-contracts.md" in text and "detected by the harness" in text

    # a MERE MENTION does not release the hold (reviewer ruling 1): naming the file while
    # describing a problem is not permission to change it
    task = task_service.get(session, task.id)
    task.definition_of_done = [
        "the changed-file set is intersected with docs/frozen-contracts.md and reported"
    ]
    session.add(task)
    session.commit()
    assert project_service.contract_findings(task, project, wt)["unauthorized"] == [
        "docs/frozen-contracts.md"
    ]

    # an EXPLICIT GRANT releases it (recomputed, same worktree)
    task = task_service.get(session, task.id)
    task.definition_of_done = [
        "authorized to amend docs/frozen-contracts.md to reword the frozen event list"
    ]
    session.add(task)
    session.commit()
    again = project_service.contract_findings(task, project, wt)
    assert again["unauthorized"] == [] and again["authorized"] == ["docs/frozen-contracts.md"]


def swept_short(session, task) -> str:
    """The one recovery commit's short sha, read from history."""
    commits = project_service.recovery_commits_for(session, task)
    assert len(commits) == 1
    return commits[0].short


def test_normal_completion_is_a_terminal_event_and_needs_no_recovery(session, tmp_path):
    project = _project(session, tmp_path, "green")
    task = _task(session, project, ["mock"], 1, dod=("MOCK_CHANGE.md updated",))
    outcome = _run(session, task)
    run = session.get(Run, outcome["run_id"])
    assert task.status == TaskStatus.accepted and task.verification_status == "pass"
    events = [e.event for e in log_service.recent(session, task_id=task.id, limit=300)]
    assert "run_completed" in events
    assert "run_failed" not in events and "run_timeout" not in events
    assert "recovery_commit" not in events  # a clean exit sweeps nothing
    done = _events(session, task, "run_completed")
    assert f"{run.short_id} finished normally (exit {run.exit_code})" in done[0].details
    assert done[0].run_id == run.id and done[0].task_id == task.id
    assert git_service.recovery_commits(Path(run.worktree_path), project.base_branch) == []


def test_retry_keeps_the_verdict_to_act_on_then_resets_it_for_the_new_round(
    session, monkeypatch, tmp_path
):
    project = _project(session, tmp_path, "rejecting")
    settings_service.update(session, {"max_review_rounds": 3})
    task = _task(session, project, ["mock", "mock-reject"], 1, dod=("MOCK_CHANGE.md updated",))
    outcome = _run(session, task)
    assert outcome["verdict"] == "CHANGES_REQUESTED" and task.status == TaskStatus.queued
    # A requeue keeps the verdict the Worker must act on; hiding it would lose the feedback.
    assert task.review_status == "CHANGES_REQUESTED" and task.verification_status == "pass"

    seen = _header_spy(session, task, monkeypatch)
    outcome = _run(session, task)  # a retry reuses the same branch and worktree
    assert seen["status"] == TaskStatus.in_progress, seen
    assert seen["handoff_status"] is None and seen["review_status"] is None, seen
    assert seen["verification_status"] is None and seen["merge_status"] is None, seen
    assert outcome["verdict"] == "CHANGES_REQUESTED"  # the new round records its own verdict
