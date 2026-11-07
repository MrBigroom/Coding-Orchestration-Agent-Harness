"""T-16 DoD 7/8: fund the evidence, and keep the gate's own cost bounded.

These are offline tests: they build unified diffs as text and never call git, a provider or the
network. The point of the first pair is that a source-heavy diff can no longer starve the files
that make its claims checkable, and that an evidence bundle which still cannot fit produces an
explicit harness finding rather than an empty diff a reviewer reads as "missing".
"""

from __future__ import annotations

import subprocess

from orchestrator.core.schemas import TaskCreate
from orchestrator.services import prompt_builder, review_service, task_service
from orchestrator.services.prompt_builder import DynamicState
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


def test_undelivered_compensation_uses_the_final_text_not_only_the_records():
    """Finding 5: a later context-budget cut makes an unchanged recorder row insufficient."""
    path = "verify-evidence/T-16/index.txt"
    patch = _chunk(path, 10, "evidence")
    assert prompt_builder.undelivered_compensation([], rendered=patch, patch=patch) == []
    assert prompt_builder.undelivered_compensation(
        [], rendered="the diff block was removed by the context budget", patch=patch
    ) == [path]


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


def test_the_worktree_guard_detects_a_transient_add_and_prune(tmp_path):
    """A start/end listing comparison is blind to add+prune; the fingerprint must not be."""
    repo = conftest.make_repo(tmp_path / "transient-repo")
    before = conftest.worktree_fingerprint(repo)
    tree = tmp_path / "transient-tree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), "HEAD"],
        check=True, capture_output=True, text=True, timeout=120,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        check=True, capture_output=True, text=True, timeout=120,
    )
    after = conftest.worktree_fingerprint(repo)
    # The old guard compared only these listings, which are identical after the prune.
    assert before["listing"] == after["listing"]
    assert conftest.worktree_fingerprint_changed(before, after) is True
