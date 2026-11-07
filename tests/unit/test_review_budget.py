"""T-16 DoD 7/8: fund the evidence, and keep the gate's own cost bounded.

These are offline tests: they build unified diffs as text and never call git, a provider or the
network. The point of the first pair is that a source-heavy diff can no longer starve the files
that make its claims checkable, and that an evidence bundle which still cannot fit produces an
explicit harness finding rather than an empty diff a reviewer reads as "missing".
"""

from __future__ import annotations

import subprocess

import pytest

from orchestrator.core.models import Review, ReviewVerdict
from orchestrator.core.schemas import ReviewResult, TaskCreate
from orchestrator.services import (
    prompt_builder,
    review_audit,
    review_service,
    run_service,
    task_service,
)
from orchestrator.services.prompt_builder import DynamicState
from orchestrator.utils.redaction import redact
from tests import conftest


def _chunk(path: str, lines: int, tag: str = "line") -> str:
    body = "\n".join(f"+{tag} {i} for a generated diff body" for i in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,0 +1,{lines} @@\n{body}\n"
    )


def _record(recorder, path: str) -> dict:
    row = next(r.as_dict() for r in recorder.records if r.item == f"diff:{path}")
    return row


def test_compensation_artifacts_get_a_reserved_slice_and_are_not_tail_dropped():
    source = "".join(_chunk(f"src/s{i}.py", 150, "source") for i in range(4))
    evidence_paths = [f"verify-evidence/T-16/e{i}.txt" for i in range(2)]
    evidence = "".join(_chunk(path, 5, "evidence") for path in evidence_paths)
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(source + evidence, 900, recorder)
    assert "compensation artifact(s)" in rendered
    # The evidence is in the reviewer-visible text, not only in the omitted list.
    for path in evidence_paths:
        assert f"+++ b/{path}" in rendered
        # A file included whole has no truncation row; if it has one it must carry bytes.
        rows = [r.as_dict() for r in recorder.records if r.item == f"diff:{path}"]
        assert not rows or rows[0]["bytes_included"] > 0
    # The source paid for the reserve: at least one source file is cut instead.
    omitted = rendered.split("omitted (", 1)[1].split("]", 1)[0]
    assert "src/s" in omitted
    assert prompt_builder.undelivered_compensation(
        [r.as_dict() for r in recorder.records]
    ) == []


def test_a_large_first_evidence_log_does_not_starve_later_evidence():
    """Review round 1 finding 1: the allocation is fair, not greedy.

    The old rule let the first evidence file take `remaining - 60 * left`, which left later files
    below the minimum and omitted them even though a fair share each would have carried them.
    """
    paths = [f"verify-evidence/T-16/e{i}.txt" for i in range(6)]
    patch = _chunk(paths[0], 2000, "huge") + "".join(
        _chunk(path, 50, "evidence") for path in paths[1:]
    )
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 2000, recorder)
    omitted = rendered.split("omitted (", 1)[1].split("]", 1)[0]
    for path in paths[1:]:
        assert path not in omitted, rendered
        assert f"+++ b/{path}" in rendered, (path, rendered[:1000])
    # The huge first log was not allowed to consume the whole group either.
    assert "verify-evidence/T-16/e0.txt" in rendered.split("partially included (", 1)[1]
    assert prompt_builder.undelivered_compensation(
        [r.as_dict() for r in recorder.records]
    ) != []


def test_an_unfundable_group_does_not_give_the_whole_budget_to_the_first_file():
    """The sub-minimum branch must still not be greedy when every file cannot be funded."""
    paths = [f"verify-evidence/T-16/u{i}.txt" for i in range(6)]
    patch = _chunk(paths[0], 2000, "huge") + "".join(
        _chunk(path, 50, "evidence") for path in paths[1:]
    )
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 200, recorder)
    # A fair minimum for the next file, not the entire group for file 0 (which the old greedy
    # branch would have taken, leaving every later file omitted).
    assert f"+++ b/{paths[1]}" in rendered, rendered


def test_undelivered_compensation_uses_the_final_text_not_only_the_records():
    """Finding 5: a later context-budget cut makes an unchanged recorder row insufficient."""
    path = "verify-evidence/T-16/index.txt"
    patch = _chunk(path, 10, "evidence")
    assert prompt_builder.undelivered_compensation([], rendered=patch, patch=patch) == []
    assert prompt_builder.undelivered_compensation(
        [], rendered="the diff block was removed by the context budget", patch=patch
    ) == [path]


def test_a_redacted_evidence_file_is_delivered_not_reported_as_a_budget_cut():
    """Review round 2 finding 1: redaction must not masquerade as an undelivered file."""
    path = "verify-evidence/T-16/secret.txt"
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwx"
    patch = (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        "@@ -1,0 +1,2 @@\n"
        f"+ANTHROPIC_API_KEY={secret}\n"
        "+ordinary evidence line\n"
    )
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 5000, recorder)
    assert path in rendered
    redacted = redact(rendered)
    assert secret not in redacted
    records = [r.as_dict() for r in recorder.records]
    # The file reached the reviewer and was rewritten in place: not a shortfall.
    assert prompt_builder.undelivered_compensation(records, rendered=redacted, patch=patch) == []
    # A real loss (the whole block gone) is still a shortfall.
    assert prompt_builder.undelivered_compensation(records, rendered="", patch=patch) == [path]


def test_manifest_shortfall_is_computed_from_the_as_sent_text():
    """Review round 2 finding 3: the archived manifest must say what the finding says."""
    path = "verify-evidence/T-16/index.txt"
    patch = _chunk(path, 20, "evidence")
    recorder = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(patch, 5000, recorder)
    records = [r.as_dict() for r in recorder.records]
    # It fits, so neither the record-only nor the as-sent view has a shortfall.
    assert "Compensation-artifact shortfall" not in prompt_builder.render_truncation_manifest(records)
    # enforce_budget then removes the whole diff block: the as-sent manifest must name it.
    manifest = prompt_builder.render_truncation_manifest(records, rendered="", patch=patch)
    assert "**Compensation-artifact shortfall:**" in manifest
    assert path in manifest
    assert prompt_builder.undelivered_compensation(records, rendered="", patch=patch) == [path]
    # The archived payload uses the same as-sent inputs, so the manifest a reviewer reads names
    # the file the harness finding names (review round 2, finding 3).
    payload = review_service.render_review_payload(
        input_text="reviewer input",
        truncations=records,
        harness_missing=[],
        reviewer_missing=[],
        absence_checks=[],
        diff_issues=[],
        supplied=review_audit.SuppliedEvidence(),
        universe=review_audit.PathUniverse(),
        rendered_diff="",
        patch_text=patch,
    )
    assert "**Compensation-artifact shortfall:**" in payload
    assert path in payload


def test_evidence_delivery_finding_never_downgrades_or_asks_the_worker_to_trim(session):
    """DoD 8(a): the harness's own budget failure is not a Worker action item.

    The finding stays visible in review.json, but the reviewer's verdict is untouched and the
    feedback string the next Worker prompt receives excludes it, so no round is spent on an
    evidence-compression commit.
    """
    result = ReviewResult(verdict=ReviewVerdict.APPROVED, findings=[])
    finding = review_service.apply_evidence_delivery_finding(
        result, ["verify-evidence/T-16/big.txt"]
    )
    assert finding is not None and finding in result.findings
    assert result.verdict == ReviewVerdict.APPROVED
    assert review_service.is_harness_budget_finding(finding)

    task = task_service.create(session, TaskCreate(title="budget feedback"))
    session.add(
        Review(
            task_id=task.id,
            verdict="CHANGES_REQUESTED",
            findings=[
                finding.model_dump(),
                {
                    "file_or_area": "src/app.py",
                    "issue": "the real code finding",
                    "violates": "DoD 1",
                },
            ],
        )
    )
    session.commit()
    feedback = run_service._latest_feedback(session, task)
    assert feedback is not None
    assert "src/app.py" in feedback and "the real code finding" in feedback
    assert "harness-detected evidence delivery" not in feedback
    assert "big.txt" not in feedback


def test_over_budget_evidence_bundle_reports_a_shortfall_and_the_exact_finding():
    """DoD 7/8(a): either the evidence reaches the reviewer, or this finding does."""
    evidence_paths = [f"verify-evidence/T-16/big{i}.txt" for i in range(8)]
    patch = "".join(_chunk(path, 220, "evidence") for path in evidence_paths)
    recorder = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(patch, 400, recorder)
    undelivered = prompt_builder.undelivered_compensation(
        [r.as_dict() for r in recorder.records]
    )
    assert undelivered, "the fixture must exceed the reserved evidence budget"
    manifest = prompt_builder.render_truncation_manifest(
        [r.as_dict() for r in recorder.records]
    )
    assert "**Compensation-artifact shortfall:**" in manifest
    for path in undelivered:
        assert path in manifest
    finding = review_service.evidence_delivery_finding(undelivered)
    assert finding is not None
    assert finding.file_or_area == ", ".join(undelivered)
    assert finding.issue.startswith("harness-detected evidence delivery:")
    assert "not missing Worker evidence" in finding.issue
    # The finding is a harness budget gate, so its text must not be pinned to this task's DoD.
    assert "T-16" not in finding.violates
    assert "compensation artifacts must be funded or reported" in finding.violates
    assert review_service.evidence_delivery_finding([]) is None


class _FakeItem:
    def __init__(self) -> None:
        self.keywords = {"parity"}
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


def test_parity_scenario_is_skipped_by_default_and_enabled_on_demand(monkeypatch):
    """DoD 8(b): the gated suite does not pay for the parity scenario unless asked."""
    monkeypatch.delenv(conftest.PARITY_ENV, raising=False)
    skipped = _FakeItem()
    conftest.pytest_collection_modifyitems(None, [skipped])
    assert len(skipped.markers) == 1
    assert getattr(skipped.markers[0], "name", "") == "skip"

    monkeypatch.setenv(conftest.PARITY_ENV, "1")
    included = _FakeItem()
    conftest.pytest_collection_modifyitems(None, [included])
    assert included.markers == []

    # Only the expensive scenarios are marked; the cheap hermeticity checks remain gated.
    from tests.integration import test_env_hermeticity

    expensive = test_env_hermeticity.test_affected_subset_is_identical_with_and_without_a_seeded_dotenv
    cheap = test_env_hermeticity.test_verification_json_names_the_gated_environment
    assert any(getattr(m, "name", "") == "parity" for m in expensive.pytestmark)
    assert not any(getattr(m, "name", "") == "parity" for m in getattr(cheap, "pytestmark", []))


def test_a_gate_review_setup_does_not_touch_the_operator_worktree_registry(session):
    """DoD 8(c): registration changes belong to temp repos, never the operator's checkout."""
    before = conftest.operator_repo_worktree_fingerprint()
    task = task_service.create(session, TaskCreate(title="P", definition_of_done=["A"]))
    built = prompt_builder.build_prompt(
        session,
        "architect",
        task,
        None,
        DynamicState(diff=_chunk("src/app.py", 5)),
    )
    assert built.rendered_diff
    after = conftest.operator_repo_worktree_fingerprint()
    assert not conftest.worktree_fingerprint_changed(before, after)


def test_the_worktree_spy_catches_a_transient_add_and_prune(tmp_path, monkeypatch):
    """Round-5 finding 1(a)/(i): the call is recorded even when the snapshot looks unchanged."""
    repo = conftest.make_repo(tmp_path / "spy-repo")
    common = conftest._git_common_dir(repo)
    assert common is not None
    spy = conftest.WorktreeMutationSpy(common)
    spy.install(monkeypatch)
    before = conftest.worktree_fingerprint(repo)
    tree = tmp_path / "spy-tree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    after = conftest.worktree_fingerprint(repo)
    # The start/end snapshot is identical after the prune: only the spy can see the mutation.
    assert not conftest.worktree_fingerprint_changed(before, after)
    flat = [" ".join(record) for record in spy.records]
    assert any("worktree add" in call for call in flat), spy.records
    assert any("worktree prune" in call for call in flat), spy.records


def test_mtime_only_git_activity_does_not_change_the_compared_snapshot(tmp_path):
    """Round-5 finding 1(b)/(ii): ordinary shared-git-dir churn must not fail the guard."""
    repo = conftest.make_repo(tmp_path / "mtime-repo")
    common = conftest._git_common_dir(repo)
    assert common is not None
    before = conftest.worktree_fingerprint(repo)
    # An index refresh and an unrelated create+rename both touch the directory mtime.
    subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    probe = common / "guard-mtime-probe"
    probe.write_text("probe\n")
    probe.replace(common / "guard-mtime-probe-renamed")
    (common / "guard-mtime-probe-renamed").unlink()
    after = conftest.worktree_fingerprint(repo)
    assert before == after
    assert not conftest.worktree_fingerprint_changed(before, after)
    assert conftest.worktree_guard_failure(before, after, []) is None
    # mtimes are not part of the compared snapshot, so they cannot become a failure condition.
    assert "git_dir_mtime_ns" not in after
    assert "registry_mtime_ns" not in after


def test_an_unreadable_worktree_registry_fails_loudly(tmp_path, monkeypatch):
    """Round-5 finding 1(d)/(iii): a silent skip is still forbidden."""
    repo = conftest.make_repo(tmp_path / "loud-repo")
    not_a_git_dir = tmp_path / "not-a-git-dir"
    not_a_git_dir.mkdir()
    monkeypatch.setattr(conftest, "_git_common_dir", lambda path: not_a_git_dir)
    with pytest.raises(RuntimeError, match="git worktree list failed"):
        conftest.worktree_fingerprint(repo)

    monkeypatch.setattr(conftest, "_git_common_dir", lambda path: None)
    with pytest.raises(RuntimeError, match="not inside a git repository"):
        conftest.worktree_fingerprint(repo)


def test_guard_message_does_not_blame_the_suite_without_spy_evidence():
    """Round-5 finding 1(c)/(iv): names changed, no recorded call, no attribution."""
    before = {"listing": ["worktree /operator/main"], "entries": ["16"]}
    after = {
        "listing": ["worktree /operator/main", "worktree /operator/other"],
        "entries": ["16", "17"],
    }
    message = conftest.worktree_guard_failure(before, after, [])
    assert message is not None
    assert "registry entries changed during the session" in message
    assert "recorded git worktree calls were: none" in message
    assert "No cause is attributed to the suite" in message
    assert "the gated suite ran" not in message

    recorded = [["git", "-C", "/operator/main", "worktree", "add", "/tmp/tree"]]
    attributed = conftest.worktree_guard_failure(before, before, recorded)
    assert attributed is not None
    assert "recorded git worktree calls: git -C /operator/main worktree add /tmp/tree" in attributed
