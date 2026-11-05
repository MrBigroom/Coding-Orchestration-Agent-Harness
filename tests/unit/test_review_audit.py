"""Mechanical checks around reviews: absence claims and diff completeness (T-11 DoD 2/3/7)."""

from __future__ import annotations

from orchestrator.core.schemas import ReviewFinding
from orchestrator.services import prompt_builder, review_audit
from orchestrator.utils.git import run_git
from tests.conftest import make_repo, requires_git

pytestmark = requires_git


def _commit(repo, message: str) -> None:
    run_git(["-c", "user.name=w", "-c", "user.email=w@w", "add", "-A"], repo)
    run_git(["-c", "user.name=w", "-c", "user.email=w@w", "commit", "-q", "-m", message], repo)


def test_existing_file_refutes_a_missing_file_claim(tmp_path):
    repo = make_repo(tmp_path / "repo", {"src/pkg/mod.py": "x = 1\n"})
    finding = ReviewFinding(
        file_or_area="src/pkg/mod.py",
        issue="src/pkg/mod.py is missing from the branch",
        violates="DoD item 1",
    )
    checks = review_audit.check_findings([finding], repo)
    assert review_audit.finding_status(checks, 0) == "refuted"
    assert checks[0].claim == "src/pkg/mod.py"
    assert "file is tracked at the reviewed commit at src/pkg/mod.py" in checks[0].detail


def test_existing_test_name_refutes_a_missing_test_claim(tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {"tests/unit/test_present.py": "def test_present_in_the_repo():\n    assert True\n"},
    )
    finding = ReviewFinding(
        file_or_area="tests",
        issue="test_present_in_the_repo is missing entirely",
        violates="DoD item 5",
    )
    checks = review_audit.check_findings([finding], repo)
    assert review_audit.finding_status(checks, 0) == "refuted"
    assert any("test function name found" in c.detail for c in checks)


def test_absent_file_claim_stays_confirmed_and_a_vague_claim_is_unchecked(tmp_path):
    repo = make_repo(tmp_path / "repo", {"README.md": "x\n"})
    absent = ReviewFinding(
        file_or_area="src/never_there.py", issue="src/never_there.py is missing", violates=""
    )
    vague = ReviewFinding(file_or_area="tests", issue="tests are not visible", violates="")
    checks = review_audit.check_findings([absent, vague], repo)
    assert review_audit.finding_status(checks, 0) == "confirmed"
    assert review_audit.finding_status(checks, 1) == "unchecked"


def test_reviewer_missing_artifact_claim_is_checked_as_a_visibility_claim(tmp_path):
    """Existence does NOT refute a "was not supplied" claim (round-2 reviewer finding 2)."""
    repo = make_repo(tmp_path / "repo", {"docs/present.md": "# here\n"})
    checks = review_audit.check_missing_artifacts(["docs/present.md", "docs/absent.md"], repo)
    # present on disk but not part of the supplied artifacts -> unchecked, never refuted;
    # absent everywhere -> confirmed.
    assert [c.status for c in checks] == ["unchecked", "confirmed"]
    assert [c.source for c in checks] == ["missing_artifact", "missing_artifact"]

    supplied = review_audit.SuppliedEvidence(
        diff_supplied={"docs/present.md"}, changed_paths={"docs/present.md"}
    )
    checks = review_audit.check_missing_artifacts(
        ["docs/present.md", "docs/absent.md"], repo, supplied
    )
    assert [c.status for c in checks] == ["refuted", "confirmed"]
    assert "supplied to the reviewer in full" in checks[0].detail


def test_a_visibility_claim_is_refuted_by_the_supplied_patch_not_by_existence(tmp_path):
    """A withheld file that the repository contains is never confirmed absent (DoD 2)."""
    supplied = review_audit.SuppliedEvidence(
        diff_supplied={"src/app.py", "src/cut.py"},
        diff_cut={"src/cut.py": (0, 4000)},
        changed_paths={"src/app.py", "src/cut.py"},
    )
    repo = make_repo(tmp_path / "repo", {"src/app.py": "x = 1\n", "src/cut.py": "y = 2\n"})
    finding = ReviewFinding(
        file_or_area="src/cut.py",
        issue="src/cut.py is truncated in the supplied diff",
        violates="DoD 4",
    )
    checks = review_audit.check_findings([finding], repo, supplied)
    assert review_audit.finding_status(checks, 0) == "unchecked"
    assert checks[0].state == review_audit.STATE_OMITTED
    assert "omitted entirely (0 of 4000 bytes" in checks[0].detail

    full = ReviewFinding(
        file_or_area="src/app.py",
        issue="src/app.py is not visible in the supplied diff",
        violates="DoD 4",
    )
    checks = review_audit.check_findings([full], repo, supplied)
    assert review_audit.finding_status(checks, 0) == "refuted"
    assert "supplied to the reviewer in full" in checks[0].detail


def test_a_partially_supplied_file_is_the_omitted_state_not_a_confirmed_absence(tmp_path):
    supplied = review_audit.SuppliedEvidence(
        diff_supplied={"src/big.py"},
        diff_cut={"src/big.py": (900, 5000)},
        changed_paths={"src/big.py"},
    )
    repo = make_repo(tmp_path / "repo", {"src/big.py": "x = 1\n"})
    finding = ReviewFinding(
        file_or_area="src/big.py", issue="src/big.py is truncated", violates="DoD 4"
    )
    checks = review_audit.check_findings([finding], repo, supplied)
    assert review_audit.finding_status(checks, 0) == "unchecked"
    assert checks[0].state == review_audit.STATE_OMITTED
    assert "only 900 of 5000 bytes" in checks[0].detail


def test_an_unknowable_visibility_claim_is_unchecked_not_refuted(tmp_path):
    """A file outside the task's diff cannot be shown to have been supplied or withheld."""
    repo = make_repo(tmp_path / "repo", {"README.md": "x\n"})
    supplied = review_audit.SuppliedEvidence(diff_supplied={"src/app.py"})
    finding = ReviewFinding(
        file_or_area="README.md",
        issue="README.md is not visible in the supplied artifacts",
        violates="",
    )
    checks = review_audit.check_findings([finding], repo, supplied)
    assert review_audit.finding_status(checks, 0) == "unchecked"
    assert "cannot be decided mechanically" in checks[0].detail


def test_supplied_evidence_is_derived_from_the_recorder_that_cut():
    patch = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,0 +1,2 @@\n+x\n+y\n"
        "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n"
        "@@ -1,0 +1,2 @@\n+p\n+q\n"
    )
    truncations = [
        {
            "section": "dynamic_state",
            "item": "diff:src/b.py",
            "bytes_available": 300,
            "bytes_included": 0,
            "budget": "max_diff_tokens=10 tokens",
            "reason": "omitted",
        }
    ]
    supplied = review_audit.supplied_evidence(patch, truncations, {"src/a.py", "src/b.py"})
    assert supplied.diff_supplied == {"src/a.py", "src/b.py"}
    assert supplied.diff_cut == {"src/b.py": (0, 300)}
    assert supplied.section_cut is False
    assert "src/b.py (0/300 bytes)" in supplied.cut_table()
    text = review_audit.render_supplied_summary(supplied)
    assert "files with a section in the supplied patch: 2" in text
    assert "src/b.py (0/300 bytes)" in text


def test_diff_budget_names_every_omitted_file_and_records_the_manifest():
    chunks = []
    for i in range(8):
        body = "\n".join(f"+line {i} {n}" for n in range(200))
        chunks.append(
            f"diff --git a/src/f{i}.py b/src/f{i}.py\n"
            f"--- a/src/f{i}.py\n+++ b/src/f{i}.py\n@@ -1,0 +1,200 @@\n{body}\n"
        )
    patch = "".join(chunks)
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 500, recorder)
    # The reviewer receives three names lists, not one "included" bucket (round-2 finding 4).
    full = rendered.split("included in full (", 1)[1].split("partially included (", 1)[0]
    partial = rendered.split("partially included (", 1)[1].split("omitted (", 1)[0]
    omitted = rendered.split("omitted (", 1)[1].split("]", 1)[0]
    # Source-first ordering funds the first file before the last one, and no file is silently
    # dropped: each one is either fully included, named as partial, or named as omitted.
    assert "src/f0.py (1 of 1 hunks, body cut inside the shown hunk)" in partial
    assert any(f"src/f{i}.py" in omitted for i in range(1, 8))
    assert "src/f0.py" not in full  # it is named once, as partial
    records = [r.as_dict() for r in recorder.records]
    assert any(
        r["item"].startswith("diff:src/") and r["bytes_available"] > r["bytes_included"]
        for r in records
    )
    manifest = prompt_builder.render_truncation_manifest(records)
    assert "| section | file / item | bytes available | bytes included | budget | reason |" in manifest
    assert "max_diff_tokens=500" in manifest


def test_a_partially_supplied_file_says_so_in_the_text_the_reviewer_reads():
    """A partial file must not be presented as if it were complete (round-2 finding 4)."""
    patch = (
        "diff --git a/src/big.py b/src/big.py\n--- a/src/big.py\n+++ b/src/big.py\n"
        "@@ -1,0 +1,400 @@\n" + "\n".join(f"+a{n}" for n in range(400)) + "\n"
        "@@ -500,0 +501,400 @@\n" + "\n".join(f"+b{n}" for n in range(400)) + "\n"
    )
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget(patch, 900, recorder)
    assert "partially included (1): src/big.py (1 of 2 hunks)" in rendered
    assert "[review diff: 1 of 2 hunks shown for src/big.py" in rendered
    # The reviewer is told which region they cannot see, not just that something is missing.
    assert "@@ -500,0 +501,400 @@" in rendered
    record = next(r.as_dict() for r in recorder.records if r.item == "diff:src/big.py")
    assert record["bytes_included"] < record["bytes_available"]
    assert "1 of 2 hunks shown" in record["reason"]


def test_an_omitted_file_is_listed_and_recorded(tmp_path):
    chunks = [
        f"diff --git a/verify-evidence/T-1/f{i}.txt b/verify-evidence/T-1/f{i}.txt\n"
        f"--- a/verify-evidence/T-1/f{i}.txt\n+++ b/verify-evidence/T-1/f{i}.txt\n"
        "@@ -1,0 +1,80 @@\n" + "\n".join(f"+line {n}" for n in range(80)) + "\n"
        for i in range(6)
    ]
    recorder = prompt_builder.TruncationRecorder()
    rendered = prompt_builder.render_diff_budget("".join(chunks), 200, recorder)
    omitted = rendered.split("omitted (", 1)[1].split("]", 1)[0]
    assert "verify-evidence/T-1/f5.txt" in omitted
    record = next(r.as_dict() for r in recorder.records if "f5.txt" in r.item)
    assert record["bytes_included"] == 0
    assert record["reason"]


def test_sensitive_files_withheld_from_the_diff_are_recorded_and_named():
    """A withheld .env must not vanish between patch.diff and the prompt (round-2 finding 3)."""
    patch = (
        "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-A=1\n+A=2\n"
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    from orchestrator.services.prompt_builder import DynamicState, _dynamic

    recorder = prompt_builder.TruncationRecorder()
    text = _dynamic(DynamicState(diff=patch), recorder)
    assert "# [sensitive file omitted]" in text
    record = next(r.as_dict() for r in recorder.records if r.item == "diff:.env")
    assert record["bytes_included"] == 0
    assert "sensitive file" in record["reason"]
    manifest = prompt_builder.render_truncation_manifest([r.as_dict() for r in recorder.records])
    assert "diff:.env" in manifest


def test_diff_completeness_flags_a_diff_attribute_introduced_by_this_task(tmp_path):
    repo = make_repo(tmp_path / "repo", {"README.md": "x\n"})
    run_git(["checkout", "-q", "-b", "task"], repo)
    evidence = repo / "verify-evidence" / "T-1"
    evidence.mkdir(parents=True)
    (evidence / "checks.txt").write_text("2 passed\n")
    (repo / ".gitattributes").write_text("verify-evidence/T-1/* -diff\n")
    _commit(repo, "add hidden evidence")
    patch = run_git(["diff", "main...HEAD"], repo).stdout

    issues = review_audit.diff_completeness(repo, "main", patch)
    attributes = [i for i in issues if i.kind == "attribute"]
    assert attributes
    assert attributes[0].path == "verify-evidence/T-1/checks.txt"
    assert attributes[0].attribute == "-diff" and attributes[0].blocks is True
    assert "-diff" in attributes[0].detail
    assert any(i.kind == "binary" and i.path.endswith("checks.txt") for i in issues)
    text = review_audit.render_diff_completeness(issues)
    assert "HARNESS FINDING" in text and "verify-evidence/T-1/checks.txt" in text


def test_diff_attribute_that_predates_the_task_is_reported_but_does_not_block(tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {"README.md": "x\n", ".gitattributes": "verify-evidence/T-1/* -diff\n"},
    )
    run_git(["checkout", "-q", "-b", "task"], repo)
    evidence = repo / "verify-evidence" / "T-1"
    evidence.mkdir(parents=True)
    (evidence / "checks.txt").write_text("2 passed\n")
    _commit(repo, "add evidence under an old attribute")
    patch = run_git(["diff", "main...HEAD"], repo).stdout

    issues = review_audit.diff_completeness(repo, "main", patch)
    attributes = [i for i in issues if i.kind == "attribute"]
    assert attributes and attributes[0].pre_existing is True
    assert attributes[0].blocks is False
    assert "pre-existing" in attributes[0].detail


def test_diff_completeness_flags_a_changed_file_that_has_no_hunk_body(tmp_path):
    repo = make_repo(tmp_path / "repo", {"src/app.py": "x = 1\n"})
    run_git(["checkout", "-q", "-b", "task"], repo)
    (repo / "src" / "app.py").write_text("x = 2\n")
    _commit(repo, "edit source")
    issues = review_audit.diff_completeness(repo, "main", "")
    assert any(i.kind == "no_hunk" and i.path == "src/app.py" and i.blocks for i in issues)
