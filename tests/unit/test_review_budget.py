"""T-16 DoD 7/8: fund the evidence, and keep the gate's own cost bounded.

These are offline tests: they build unified diffs as text and never call git, a provider or the
network. The point of the first pair is that a source-heavy diff can no longer starve the files
that make its claims checkable, and that an evidence bundle which still cannot fit produces an
explicit harness finding rather than an empty diff a reviewer reads as "missing".
"""

from __future__ import annotations

import pytest

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
    assert finding.violates == "T-16 DoD 7 (compensation artifacts must be funded or reported)"
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

    # The environment module that owns the expensive scenarios really is marked.
    from tests.integration import test_env_hermeticity

    assert any(getattr(m, "name", "") == "parity" for m in test_env_hermeticity.pytestmark)


def test_a_gate_review_setup_does_not_touch_the_operator_worktree_list(session):
    """DoD 8(c): registration changes belong to temp repos, never the operator's checkout."""
    before = conftest.operator_repo_worktrees()
    if not before:
        pytest.skip("not running inside a git checkout")
    task = task_service.create(session, TaskCreate(title="P", definition_of_done=["A"]))
    built = prompt_builder.build_prompt(
        session,
        "architect",
        task,
        None,
        DynamicState(diff=_chunk("src/app.py", 5)),
    )
    assert built.rendered_diff
    after = conftest.operator_repo_worktrees()
    assert after == before
