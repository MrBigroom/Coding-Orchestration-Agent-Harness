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
from orchestrator.utils.git import run_git
from orchestrator.utils.redaction import redact
from orchestrator.utils.truncate import estimate_tokens, truncate_middle
from tests import conftest
from tests.conftest import make_repo, requires_git


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


# --- T-36: the allocator must satisfy the invariant the diff auditor enforces. -----------------

R68_FILES: tuple[tuple[str, int], ...] = (
    ("src/orchestrator/services/large_generated.py", 1400),
    ("README.md", 50),
    ("docs/build-loop.md", 180),
    ("docs/decisions.md", 140),
    ("docs/frozen-contracts.md", 60),
    ("docs/memory.md", 80),
    ("scripts/t15_assertion_shape.py", 160),
    ("tests/unit/test_evidence_freshness.py", 800),
    ("tests/unit/test_memory_scopes.py", 500),
    *((f"src/orchestrator/services/module_{i}.py", 60 + i) for i in range(8)),
    *((f"tests/unit/test_module_{i}.py", 80 + i) for i in range(5)),
    *((f"docs/note_{i}.md", 30 + i) for i in range(4)),
    *((f"scripts/t36_script_{i}.py", 40 + i) for i in range(2)),
    *((f"web/src/module_{i}.ts", 45 + i) for i in range(2)),
)


def _r68_patch() -> str:
    """The R-68 shape: one ~43KB source file plus 29 smaller files, docs and tests included."""
    assert len(R68_FILES) == 30
    return "".join(_chunk(path, lines, "r68-generated-body-line") for path, lines in R68_FILES)


def _multi_hunk_chunk(path: str, hunk_lines: tuple[int, ...], tag: str = "line") -> str:
    """A file with several hunks, so a token target can land between hunk boundaries."""
    parts = [f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"]
    start = 1
    for n, lines in enumerate(hunk_lines):
        body = "\n".join(
            f"+{tag} {n}.{i} for a generated diff body" for i in range(lines)
        )
        parts.append(f"@@ -{start},0 +{start},{lines} @@\n{body}\n")
        start += lines
    return "".join(parts)


def _list_section(rendered: str, name: str) -> str:
    return rendered.split(f"{name} (", 1)[1].split("]", 1)[0]


def _commit_repo(repo, message: str) -> None:
    run_git(["-c", "user.name=w", "-c", "user.email=w@w", "add", "-A"], repo)
    run_git(["-c", "user.name=w", "-c", "user.email=w@w", "commit", "-q", "-m", message], repo)


def test_r68_fixture_gets_a_hunk_for_every_file_at_the_default_budget():
    """DoD 1/5(a): the measured R-68 starvation shape is the named fixture."""
    patch = _r68_patch()
    assert len(prompt_builder.split_diff_files(patch)[0][1].encode("utf-8")) > 43_000
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(
        patch, get_settings_default_max_diff_tokens(), recorder
    )
    chunks = dict(prompt_builder.split_diff_files(rendered))
    missing = [path for path, _lines in R68_FILES if path not in chunks]
    no_hunk = [
        path
        for path, _lines in R68_FILES
        if path in chunks
        and not any(line.startswith("@@") for line in chunks[path].splitlines())
    ]
    assert missing == [], missing
    assert no_hunk == [], no_hunk
    rows = {
        record.as_dict()["item"]: record.as_dict()
        for record in recorder.records
        if str(record.item).startswith("diff:")
    }
    zero_rows = [item for item, row in rows.items() if row["bytes_included"] == 0]
    assert zero_rows == [], zero_rows
    assert _list_section(rendered, "omitted").strip() == "0): none"


def test_remainder_moves_past_a_hunk_boundary_instead_of_being_stranded():
    """DoD 2/3: a target that renders short hands the difference to later files.

    `src/a.py`'s first hunk is tiny and its second hunk is far larger than its allocation, so
    `_chunk_head` delivers far less than the token target it was given. The undelivered target
    must go back to the group: before T-36 review finding 1 the allocator kept it reserved for
    `a.py`, and `src/c.py` was left at its floor even though the group still had tokens. This
    fails on that allocator (c.py got only its floor) and passes once allocation is by delivered
    cost.
    """
    patch = (
        _multi_hunk_chunk("src/a.py", (5, 1000), "a")
        + _chunk("src/b.py", 100, "b")
        + _chunk("src/c.py", 1000, "c")
    )
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 1200, recorder)
    chunks = dict(prompt_builder.split_diff_files(rendered))
    floor = prompt_builder.DIFF_FILE_FLOOR_TOKENS
    # a.py's allocated target could not buy its second hunk; it received only its first.
    assert estimate_tokens(chunks["src/a.py"]) < floor
    # The tokens a.py could not take were not stranded: the last file gets well past its floor.
    assert estimate_tokens(chunks["src/c.py"]) > 2 * floor
    # And the group is not reported as exhausted while it still had tokens that could be spent.
    rows = [record.as_dict() for record in recorder.records]
    stranded = [
        row
        for row in rows
        if "floor only, remainder exhausted" in row["reason"]
    ]
    assert stranded == [], stranded


def test_forty_files_keep_their_floor_at_the_default_budget_even_with_long_paths():
    """DoD 1: the floor is sized for at least 40 files under the REDUCED source share.

    The fixture contains `verify-evidence/` files on purpose. With compensation artifacts present
    the source group gets `11700 * 2/3 = 7800` tokens, not the full 11700, and 40 floors cost
    `40 * 160 = 6400`. Without an evidence file this test would exercise 11700 and prove nothing
    about the arithmetic the docs state (T-36 review finding 2).
    """
    paths = [f"src/{'long_path_' + 'a' * 100}_{i}.py" for i in range(40)]
    assert len(paths) == 40
    evidence_paths = [f"verify-evidence/T-36/e{i}.txt" for i in range(2)]
    patch = "".join(_chunk(path, 400, "forty") for path in paths + evidence_paths)
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(
        patch, get_settings_default_max_diff_tokens(), recorder
    )
    # The compensation reserve is in force, so the source share really is 7800 and not 11700.
    assert "40 source file(s) share 7800 tokens" in rendered, rendered.splitlines()[0]
    chunks = dict(prompt_builder.split_diff_files(rendered))
    missing = [
        path
        for path in paths + evidence_paths
        if path not in chunks
        or not any(line.startswith("@@") for line in chunks[path].splitlines())
    ]
    assert missing == [], missing
    assert _list_section(rendered, "omitted").strip() == "0): none"


def test_no_floor_render_costs_more_than_the_floor_even_for_a_long_path():
    """DoD 1: `DIFF_FILE_FLOOR_TOKENS * file_count` must be a real upper bound.

    `_floor_head` used to clamp its body to `max(24, ...)`, so a long path made one floor cost
    ~200 tokens and the stated arithmetic (`40 * 160`) was only true on average. Every rendered
    floor is now capped by `estimate_tokens`, and a path too long for any variant is reported as
    omitted instead of overflowing the floor.
    """
    floor = prompt_builder.DIFF_FILE_FLOOR_TOKENS
    for path_length in (10, 60, 120, 200, 280):
        path = f"src/{'p' * path_length}.py"
        chunk = _chunk(path, 400, "cap")
        rendered, _shown, _total, sliced = prompt_builder._floor_head(chunk, floor, path)
        assert sliced is True
        assert estimate_tokens(rendered) <= floor, (path_length, estimate_tokens(rendered))
        assert any(line.startswith("@@") for line in rendered.splitlines()), path_length
    # A path whose own header cannot fit inside the floor is not silently given bytes: the
    # renderer returns "" and the allocator names it omitted with that reason.
    huge = f"src/{'p' * 400}.py"
    assert prompt_builder._floor_head(_chunk(huge, 400, "cap"), floor, huge)[0] == ""
    recorder = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(_chunk(huge, 400, "cap"), 500, recorder)
    row = next(r.as_dict() for r in recorder.records if r.item == f"diff:{huge}")
    assert row["bytes_included"] == 0
    assert "no floor render fits inside the 160-token floor" in row["reason"], row["reason"]

    # Mutation: the pre-fix renderer, whose body budget was only clamped at a floor of 24 tokens,
    # overflows the floor on the same input. The cap above is what bounds it, not the path text.
    long_path = f"src/{'p' * 280}.py"
    legacy = _old_floor_head_for_mutation_check(
        _chunk(long_path, 400, "cap"), floor, long_path
    )
    assert estimate_tokens(legacy) > floor, estimate_tokens(legacy)


def _old_floor_head_for_mutation_check(chunk: str, max_tokens: int, path: str) -> str:
    """The T-36 first implementation, kept verbatim so the mutation above is real."""
    lines = chunk.splitlines(keepends=True)
    hunk_starts = [i for i, line in enumerate(lines) if line.startswith("@@")]
    header = "".join(lines[: hunk_starts[0]])
    hunks = [
        "".join(lines[start : (hunk_starts[n + 1] if n + 1 < len(hunk_starts) else len(lines))])
        for n, start in enumerate(hunk_starts)
    ]
    marker = (
        f"\n... [review diff floor: {path or 'this file'} stopped at its first hunk; "
        "the remainder did not fit after every file's floor — see the truncation manifest] ...\n"
    )
    if path and estimate_tokens(header) + estimate_tokens(marker) + 24 > max_tokens:
        header = f"diff --git a/{path} b/{path}\n"
    body_budget = max(24, max_tokens - estimate_tokens(header) - estimate_tokens(marker))
    return header + truncate_middle(hunks[0], body_budget) + marker


def test_floor_only_reason_says_whether_the_budget_ran_out_or_the_cap_held():
    """DoD 3: 'remainder exhausted' must not be claimed when the group still had tokens.

    Two scenarios, one assertion each, because the two outcomes are the point. In the first the
    group budget is fully committed before the last floor is funded; in the second every floor is
    funded and the per-file `cap` (below the floor) is what stops the top-up, so tokens stay
    unallocated under the file's own share.
    """
    exhausted = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(
        "".join(_chunk(f"src/x{i}.py", 400, "x") for i in range(4)), 1000, exhausted
    )
    exhausted_reasons = [
        r.as_dict()["reason"] for r in exhausted.records if r.as_dict()["kind"] == "budget"
    ]
    assert any("floor only, remainder exhausted" in reason for reason in exhausted_reasons)
    assert not any("floor only, " in reason and "exhausted" not in reason for reason in exhausted_reasons)

    capped = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(
        "".join(_chunk(f"src/c{i}.py", 400, "c") for i in range(2)), 300, capped
    )
    capped_reasons = [
        r.as_dict()["reason"] for r in capped.records if r.as_dict()["kind"] == "budget"
    ]
    assert any("floor only, share under the floor" in reason for reason in capped_reasons), (
        capped_reasons
    )
    assert not any("floor only, remainder exhausted" in reason for reason in capped_reasons)
    assert not any("hunk-granular" in reason for reason in capped_reasons)


def _assert_reason_names_the_hunk_cause(reason: str, leftover: int, per_file: int, cap: int) -> None:
    """The false share claim is gone and the real numbers are in the string (T-36 round 7)."""
    assert "did not exceed that floor" not in reason, reason
    assert "went to other files" not in reason, reason
    assert "hunk-granular" in reason, reason
    assert "stayed unallocated" in reason, reason
    for number in (leftover, per_file, cap, prompt_builder.DIFF_FILE_FLOOR_TOKENS):
        assert str(number) in reason, (number, reason)


def _assert_floor_guarantee(patch: str, rendered: str, recorder) -> None:
    """Every diffstat file has bytes and a hunk, and no manifest row reports zero bytes."""
    paths = [path for path, _chunk in prompt_builder.split_diff_files(patch)]
    shown = dict(prompt_builder.split_diff_files(rendered))
    missing = [path for path in paths if path not in shown or not shown[path]]
    no_hunk = [
        path
        for path in paths
        if path in shown
        and not any(line.startswith("@@") for line in shown[path].splitlines())
    ]
    assert missing == [], missing
    assert no_hunk == [], no_hunk
    zero_rows = [
        record.as_dict()["item"]
        for record in recorder.records
        if record.as_dict()["kind"] == "budget"
        and str(record.item).startswith("diff:")
        and record.as_dict()["bytes_included"] == 0
    ]
    assert zero_rows == [], zero_rows


def _source_leftover(rendered: str, patch: str, source_budget: int) -> int:
    """Source-group tokens the allocation passes did not deliver, measured on the as-sent text."""
    body = rendered.split("]\n", 1)[1]
    shown = dict(prompt_builder.split_diff_files(body))
    paths = [
        path
        for path, _chunk in prompt_builder.split_diff_files(patch)
        if not prompt_builder.is_compensation_artifact(path)
    ]
    delivered = sum(estimate_tokens(shown[path]) for path in paths if path in shown)
    return source_budget - delivered


def _own_branch_shape_patch() -> str:
    """Seven source files plus five compensation artifacts: per_file 1114, cap 3900."""
    source = [_multi_hunk_chunk(f"src/part_{i}.py", (3, 20000), f"p{i}") for i in range(7)]
    evidence = [_chunk(f"verify-evidence/T-36/e{i}.txt", 30 + i) for i in range(5)]
    return "".join(source + evidence)


def _r68_sparse_patch() -> str:
    """The R-68 shape at the default budget with a real leftover: 30 source files, per_file 260."""
    files = [
        ("src/orchestrator/services/large_generated.py", 1400),
        ("README.md", 10),
        ("docs/build-loop.md", 15),
        ("docs/decisions.md", 15),
        ("docs/frozen-contracts.md", 8),
        ("docs/memory.md", 12),
        ("scripts/t15_assertion_shape.py", 18),
        ("tests/unit/test_evidence_freshness.py", 20),
        ("tests/unit/test_memory_scopes.py", 15),
        *((f"src/orchestrator/services/module_{i}.py", 6 + i) for i in range(8)),
        *((f"tests/unit/test_module_{i}.py", 7 + i) for i in range(5)),
        *((f"docs/note_{i}.md", 5 + i) for i in range(4)),
        *((f"scripts/t36_script_{i}.py", 6 + i) for i in range(2)),
        *((f"web/src/module_{i}.ts", 7 + i) for i in range(2)),
    ]
    assert len(files) == 30
    patch = "".join(_chunk(path, lines, "r68") for path, lines in files)
    return patch + _chunk("verify-evidence/T-36/r68.txt", 10)


def _forty_nine_file_default_budget_patch() -> str:
    """The default-budget shape that separates the fair share from the effective ceiling.

    A short path with a (12, 20000) hunk pair renders a 155-token first-hunk floor with the first
    hunk fully shown, and the next hunk cannot fit even at the 3900-token cap. Forty-nine such
    floors cost 7781 of the 7800-token source share; the top-up pass spends 18 of the 19 leftover
    tokens in marker-sized increments and one token remains. `per_file` is 159 -- below the
    160-token floor -- while `cap` is 3900, which is the exact contradiction round 8 finding 1
    named. The compensation artifact keeps the T-16 source share at 7800 rather than 11700.
    """
    paths = [f"src/m{i}.py" for i in range(49)]
    source = "".join(_multi_hunk_chunk(path, (12, 20000), "m") for path in paths)
    return source + _chunk("verify-evidence/T-36/r8.txt", 10)


def test_own_branch_shape_reason_names_the_hunk_cause_and_keeps_every_floor():
    """T-36 round 7: the measured own-branch shape must not claim its share was under the floor.

    R-71 measured this branch's own source group at per_file=1114, cap=3900 with 1769 tokens
    unspent. The old string called that share "did not exceed" the 160-token floor and said the
    unallocated tokens "went to other files". Both claims are false and the second contradicts
    `leftover > 0` by construction.
    """
    measured = prompt_builder._floor_only_reason(1769, 1114, 3900)
    _assert_reason_names_the_hunk_cause(measured, 1769, 1114, 3900)

    patch = _own_branch_shape_patch()
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(
        patch, get_settings_default_max_diff_tokens(), recorder
    )
    assert "7 source file(s) share 7800 tokens (per-file 1114, cap 3900)" in rendered, (
        rendered.splitlines()[0]
    )
    leftover = _source_leftover(rendered, patch, 7800)
    assert leftover > 0, leftover
    _assert_reason_names_the_hunk_cause(
        prompt_builder._floor_only_reason(leftover, 1114, 3900), leftover, 1114, 3900
    )
    _assert_floor_guarantee(patch, rendered, recorder)


def test_r68_shape_reason_names_the_hunk_cause_and_keeps_every_floor():
    """T-36 round 7: the R-68 shape (30 files, one ~43KB, per_file 260) gets the real cause."""
    patch = _r68_sparse_patch()
    large = dict(prompt_builder.split_diff_files(patch))[
        "src/orchestrator/services/large_generated.py"
    ]
    assert len(large.encode("utf-8")) > 43_000, len(large.encode("utf-8"))
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(
        patch, get_settings_default_max_diff_tokens(), recorder
    )
    assert "30 source file(s) share 7800 tokens (per-file 260, cap 3900)" in rendered, (
        rendered.splitlines()[0]
    )
    leftover = _source_leftover(rendered, patch, 7800)
    assert leftover > 0, leftover
    reason = prompt_builder._floor_only_reason(leftover, 260, 3900)
    _assert_reason_names_the_hunk_cause(reason, leftover, 260, 3900)
    _assert_floor_guarantee(patch, rendered, recorder)


def test_default_budget_49_file_shape_emits_granularity_not_the_share_claim():
    """Round-8 finding 1: compare the effective ceiling, not `per_file` alone.

    The 49-file shape has `per_file=159` (under the floor) but `cap=3900` (far above it), so the
    old `per_file <= DIFF_FILE_FLOOR_TOKENS` branch printed "share (159 tokens, cap 3900) did not
    exceed that floor" -- a sentence contradicted by its own numbers. The allocator-emitted row
    must be the granularity wording, and it must carry the measured per_file/cap/floor/leftover.
    """
    patch = _forty_nine_file_default_budget_patch()
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(
        patch, get_settings_default_max_diff_tokens(), recorder
    )
    assert "49 source file(s) share 7800 tokens (per-file 159, cap 3900)" in rendered, (
        rendered.splitlines()[0]
    )
    leftover = _source_leftover(rendered, patch, 7800)
    assert leftover > 0, leftover
    floor_rows = [
        record.as_dict()
        for record in recorder.records
        if record.as_dict()["kind"] == "budget"
        and "floor only" in record.as_dict()["reason"]
    ]
    assert floor_rows, [record.as_dict() for record in recorder.records]
    for row in floor_rows:
        reason = row["reason"]
        assert "next hunk did not fit" in reason, reason
        assert "share under the floor" not in reason, reason
        assert "did not exceed that floor" not in reason, reason
    reason = floor_rows[0]["reason"]
    assert "(159 tokens, cap 3900, effective ceiling 3900 tokens)" in reason, reason
    assert f"the {leftover} tokens the group had left" in reason, reason
    assert f"{prompt_builder.DIFF_FILE_FLOOR_TOKENS}-token per-file floor" in reason, reason
    _assert_floor_guarantee(patch, rendered, recorder)


def test_the_allocator_emits_the_granularity_row_with_its_own_leftover():
    """Round-8 finding 2: the row must come from `render_diff_budget`, not a helper call.

    Seven multi-hunk files at a 500-token budget: the allocator funds five 155-token floors, the
    next hunk of every funded file is too large for its 210-token cap, and 5 tokens stay unspent.
    The manifest row is therefore emitted by the same code path the reviewer payload uses.
    """
    paths = [f"src/g{i}.py" for i in range(7)]
    patch = "".join(_multi_hunk_chunk(path, (3, 5000), "g") for path in paths)
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 500, recorder)
    assert "per-file budget 120 tokens (one file may use up to 210)" in rendered, (
        rendered.splitlines()[0]
    )
    leftover = _source_leftover(rendered, patch, 420)
    assert leftover > 0, leftover
    floor_rows = [
        record.as_dict()
        for record in recorder.records
        if record.as_dict()["kind"] == "budget"
        and "floor only" in record.as_dict()["reason"]
    ]
    assert floor_rows, [record.as_dict() for record in recorder.records]
    reason = floor_rows[0]["reason"]
    assert "next hunk did not fit" in reason, reason
    assert "share under the floor" not in reason, reason
    assert "(120 tokens, cap 210, effective ceiling 210 tokens)" in reason, reason
    assert f"the {leftover} tokens the group had left" in reason, reason
    assert f"{prompt_builder.DIFF_FILE_FLOOR_TOKENS}-token per-file floor" in reason, reason


def test_floor_only_reason_at_or_below_the_floor_keeps_the_share_claim():
    """The other branch of the conditional: here the share really is at or below the floor."""
    reason = prompt_builder._floor_only_reason(120, 150, 150)
    assert "floor only, share under the floor" in reason, reason
    assert "did not exceed that floor" in reason, reason
    assert "hunk-granular" not in reason, reason
    assert "went to other files" not in reason, reason
    for number in (120, 150, prompt_builder.DIFF_FILE_FLOOR_TOKENS):
        assert str(number) in reason, (number, reason)

    # End to end: two files, 300 tokens -> per_file=cap=150, below the 160-token floor.
    recorder = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(
        "".join(_chunk(f"src/c{i}.py", 400, "c") for i in range(2)), 300, recorder
    )
    emitted = [
        record.as_dict()["reason"]
        for record in recorder.records
        if record.as_dict()["kind"] == "budget" and "floor only" in record.as_dict()["reason"]
    ]
    assert emitted, [record.as_dict() for record in recorder.records]
    assert all("share under the floor" in reason for reason in emitted), emitted
    assert all("did not exceed that floor" in reason for reason in emitted), emitted


def test_the_distinct_floor_only_wording_is_what_the_allocation_decides(monkeypatch):
    """Mutation: force the old single sentence and the capped scenario loses its distinction."""
    monkeypatch.setattr(
        prompt_builder,
        "_floor_only_reason",
        lambda leftover, per_file, cap: "floor only, remainder exhausted: forced",
    )
    forced = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(
        "".join(_chunk(f"src/c{i}.py", 400, "c") for i in range(2)), 300, forced
    )
    reasons = [r.as_dict()["reason"] for r in forced.records if r.as_dict()["kind"] == "budget"]
    assert not any("share under the floor" in reason for reason in reasons), reasons
    assert any("forced" in reason for reason in reasons), reasons


def get_settings_default_max_diff_tokens() -> int:
    return prompt_builder.get_settings().max_diff_tokens


def test_a_floor_only_file_is_reported_distinctly_from_full_and_omitted():
    """DoD 3/5(b): 'floor only, remainder exhausted' is not 'omitted entirely'."""
    paths = [f"src/floor{i}.py" for i in range(4)]
    big = "".join(_chunk(path, 400, "floor") for path in paths)
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(big, 1000, recorder)
    rows = {
        record.as_dict()["item"]: record.as_dict() for record in recorder.records
    }
    floor_rows = [
        rows[f"diff:{path}"]
        for path in paths
        if f"diff:{path}" in rows and "floor only, remainder exhausted" in rows[f"diff:{path}"]["reason"]
    ]
    assert floor_rows, rows
    assert "src/floor0.py" in _list_section(rendered, "partially included")
    assert "first hunk only, floor allocation" in _list_section(rendered, "partially included")

    # A small file that fits in full has no manifest row at all; it is the other outcome.
    small_recorder = prompt_builder.TruncationRecorder()
    small_rendered = prompt_builder.render_diff_budget(
        _chunk("src/small.py", 3) + _chunk("src/large.py", 2000), 5000, small_recorder
    )
    assert "src/small.py" in _list_section(small_rendered, "included in full")
    assert not any(
        record.item == "diff:src/small.py" for record in small_recorder.records
    )

    # A budget too small for every floor omits the tail and says so explicitly.
    omitted_recorder = prompt_builder.TruncationRecorder()
    prompt_builder.render_diff_budget(big, 300, omitted_recorder)
    omitted_rows = [
        record.as_dict()
        for record in omitted_recorder.records
        if str(record.item).startswith("diff:") and record.bytes_included == 0
    ]
    assert omitted_rows, [r.as_dict() for r in omitted_recorder.records]
    assert all("omitted entirely" in row["reason"] for row in omitted_rows)


def test_the_floor_pass_leaves_the_t16_evidence_reserve_working():
    """DoD 2/5(c): a source-heavy diff still funds verify-evidence/ after the floor pass."""
    source = "".join(_chunk(f"src/big{i}.py", 400, "source") for i in range(6))
    evidence_paths = [f"verify-evidence/T-36/e{i}.txt" for i in range(4)]
    evidence = "".join(_chunk(path, 20, "evidence") for path in evidence_paths)
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(source + evidence, 12000, recorder)
    rows = {record.item: record.as_dict() for record in recorder.records}
    for path in evidence_paths:
        assert f"+++ b/{path}" in rendered, path
        row = rows.get(f"diff:{path}")
        assert row is None or row["bytes_included"] > 0, row
    assert all(path not in _list_section(rendered, "omitted") for path in evidence_paths)
    # The large source file still gets the lion's share of the remainder.
    shown = dict(prompt_builder.split_diff_files(rendered))
    assert len(shown["src/big0.py"]) > len(shown[evidence_paths[0]])


@requires_git
def test_budget_rendered_diff_has_no_blocking_completeness_issue(tmp_path):
    """DoD 5(d): the auditor accepts the text the new allocator actually emits.

    The fixture must be **bigger than the default budget**, otherwise `render_diff_budget` returns
    the patch unchanged and `diff_completeness` audits the raw patch: the allocator would never
    run and this test would pass without exercising it (T-36 review finding 1).
    """
    files = {
        f"src/module_{i}.py": "\n".join(f"old {i}.{n} with some padding text" for n in range(700))
        for i in range(5)
    }
    files["docs/guide.md"] = "\n".join(f"old doc {n} with some padding text" for n in range(700))
    files["tests/unit/test_app.py"] = "\n".join(
        f"old test {n} with some padding text" for n in range(700)
    )
    repo = make_repo(tmp_path / "repo", files)
    run_git(["checkout", "-q", "-b", "task"], repo)
    for name in files:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(
            "\n".join(f"new {i} with some padding text" for i in range(700)) + "\n"
        )
    _commit_repo(repo, "change files")
    patch = run_git(["diff", "main...HEAD"], repo).stdout
    default_budget = get_settings_default_max_diff_tokens()
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, default_budget, recorder)
    # Proof the allocator ran at all: the patch exceeds the budget, so the as-sent text is a
    # budgeted render with its preamble, not the raw patch.
    assert estimate_tokens(patch) > default_budget, estimate_tokens(patch)
    assert rendered != patch
    assert "[review diff budget:" in rendered
    # And it really truncated something, so the auditor is looking at allocator output.
    assert any(
        str(record.item).startswith("diff:") and record.bytes_included < record.bytes_available
        for record in recorder.records
    ), [record.as_dict() for record in recorder.records]
    issues = review_audit.diff_completeness(repo, "main", patch, rendered=rendered)
    assert [issue for issue in issues if issue.blocks] == [], issues


@requires_git
def test_over_budget_still_omits_a_file_and_the_auditor_still_blocks(tmp_path):
    """DoD 5(e): the auditor is not weakened to make the allocator look correct."""
    files = {f"src/f{i}.py": f"old = {i}\n" for i in range(8)}
    repo = make_repo(tmp_path / "repo", files)
    run_git(["checkout", "-q", "-b", "task"], repo)
    for name in files:
        (repo / name).write_text("\n".join(f"new {i}" for i in range(300)) + "\n")
    _commit_repo(repo, "change files")
    patch = run_git(["diff", "main...HEAD"], repo).stdout
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 300, recorder)
    omitted = _list_section(rendered, "omitted")
    assert "src/f" in omitted, omitted
    issues = review_audit.diff_completeness(repo, "main", patch, rendered=rendered)
    blocking = [issue for issue in issues if issue.blocks and issue.kind == "no_hunk"]
    assert blocking, issues
    assert any(issue.path in omitted for issue in blocking)


def test_rendering_the_same_diff_twice_is_byte_identical():
    """DoD 5(f): allocation must not depend on set order, time or a random tie-break."""
    patch = _r68_patch()
    first = prompt_builder.render_diff_budget(patch, 12000, prompt_builder.TruncationRecorder())
    second = prompt_builder.render_diff_budget(patch, 12000, prompt_builder.TruncationRecorder())
    assert first == second


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
