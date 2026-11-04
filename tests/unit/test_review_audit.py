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
    assert "exists in the task worktree" in checks[0].detail


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


def test_reviewer_missing_artifact_claim_is_checked_the_same_way(tmp_path):
    repo = make_repo(tmp_path / "repo", {"docs/present.md": "# here\n"})
    checks = review_audit.check_missing_artifacts(["docs/present.md", "docs/absent.md"], repo)
    assert [c.status for c in checks] == ["refuted", "confirmed"]
    assert [c.source for c in checks] == ["missing_artifact", "missing_artifact"]


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
    assert "omitted (" in rendered
    # At least one file did not fit, and its name is stated in the text the reviewer receives.
    included = rendered.split("included (", 1)[1].split("omitted (", 1)[0]
    omitted = rendered.split("omitted (", 1)[1].split("]", 1)[0]
    assert included.strip() != "" and omitted.strip() != "none"
    assert any(f"src/f{i}.py" in omitted for i in range(8))
    records = [r.as_dict() for r in recorder.records]
    assert any(
        r["item"].startswith("diff:src/") and r["bytes_available"] > r["bytes_included"]
        for r in records
    )
    manifest = prompt_builder.render_truncation_manifest(records)
    assert "| section | file / item | bytes available | bytes included | budget | reason |" in manifest
    assert "max_diff_tokens=500" in manifest


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
