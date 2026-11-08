"""T-15: committed evidence freshness is a deterministic equality test, not a reviewer's reading.

Offline, local git repositories only: every case builds its own history with real stamps, so the
checks run against the same shapes the repository carries (nine stamp formats, recovery commits,
operator hand-fixes, evidence-only commits, short SHAs and deleted bundles).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.services import evidence_freshness as ef
from tests.conftest import make_repo, requires_git

pytestmark = requires_git

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=ENV,
    ).stdout


def _commit(repo: Path, subject: str, files: dict[str, str], body: str | None = None) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    args = ["commit", "-q", "-m", subject]
    if body:
        args += ["-m", body]
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD").strip()


def _repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo", {"README.md": "# test\n"})


def _evidence(stamp: str, text: str = "suite: 3 passed\n") -> dict[str, str]:
    return {"verify-evidence/T-1/full_suite.txt": f"{stamp}\n{text}"}


# ------------------------------------------------------------------ parsing the stamp shapes


@pytest.mark.parametrize(
    ("line", "sha"),
    [
        (
            "# HEAD=0123456789abcdef0123456789abcdef01234567",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        (
            "# HEAD=0123456789abcdef0123456789abcdef01234567 (0123456) branch=task-4",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        (
            "# HEAD 0123456789abcdef0123456789abcdef01234567",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        ("# T-18 full suite at HEAD 0123456", "0123456"),
        ("# HEAD=0123456 tracked-dirty-entries=0", "0123456"),
        (
            "HEAD 0123456789abcdef0123456789abcdef01234567 | dirty-entries 0 | "
            "2026-11-08T00:00:00Z",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        (
            "# HEAD=0123456789abcdef0123456789abcdef01234567 "
            "tracked-and-untracked-entries-outside-this-dir=0",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        ("== commit under test: task-6 @ 0123456 ==", "0123456"),
    ],
)
def test_every_legacy_stamp_shape_is_parsed(line: str, sha: str) -> None:
    stamps, unparsed = ef.parse_stamps(line + "\nbody\n")
    assert [stamp.sha for stamp in stamps] == [sha]
    assert unparsed == []


def test_a_file_with_no_stamp_is_unstamped_not_an_error() -> None:
    stamps, unparsed = ef.parse_stamps("suite: 3 passed\n")
    assert stamps == []
    assert unparsed == []


def test_a_comment_that_merely_says_head_is_not_an_unparsable_stamp() -> None:
    text = "# Run from the repo root at the code HEAD you want to certify.\n3 passed\n"
    stamps, unparsed = ef.parse_stamps(text)
    assert stamps == []
    assert unparsed == []


def test_an_unparsable_stamp_is_reported_not_treated_as_fresh(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _commit(repo, "evidence", _evidence("# HEAD=not-a-sha"))
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.status == ef.STATUS_STALE
    assert result.stale_paths == ["verify-evidence/T-1/full_suite.txt"]
    assert result.unparsed_paths == ["verify-evidence/T-1/full_suite.txt"]
    assert "unparsable HEAD stamp" in result.notes


def test_a_short_sha_is_prefix_resolved_against_the_commit_under_verification(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    head = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    stamp = ef.EvidenceStamp(ef._short(repo, head), f"# HEAD={ef._short(repo, head)}", 1)
    ok, reason = ef._stamp_verdict(repo, stamp, head)
    assert ok is True
    assert reason == "matches the commit under verification"


# ------------------------------------------------------------------ stale / pass / not_applicable


def test_an_older_stamp_is_stale_with_the_paths_and_the_count(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old = _commit(repo, "round 4 evidence", {"verify-evidence/T-1/old.txt": "old\n"})
    _commit(
        repo,
        "round 4 evidence, stamped",
        {
            "verify-evidence/T-1/full_suite.txt": f"# HEAD={old}\n3 passed\n",
            "verify-evidence/T-1/tests.txt": f"# HEAD={old}\n2 passed\n",
        },
    )
    new = _commit(repo, "round 5 code", {"src/app.py": "def handle():\n    return 1\n"})
    result = ef.check_freshness(repo, new, "T-1", "main")
    assert result.status == ef.STATUS_STALE
    assert result.stale_count == 2
    assert result.stale_paths == [
        "verify-evidence/T-1/full_suite.txt",
        "verify-evidence/T-1/tests.txt",
    ]
    assert ef._short(repo, new) in result.evidence
    assert "non-evidence commit" in result.notes


def test_a_stamp_that_differs_only_by_an_evidence_only_commit_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    head = _commit(repo, "evidence", _evidence(f"# HEAD={first}"))
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.status == ef.STATUS_PASS
    assert "evidence-only commit" in result.notes


def test_absent_evidence_is_not_applicable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _commit(repo, "code only", {"src/app.py": "x = 1\n"})
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.status == ef.STATUS_NOT_APPLICABLE
    assert "no committed evidence" in result.evidence


def test_unstamped_evidence_is_not_applicable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _commit(
        repo,
        "evidence without a stamp",
        {"verify-evidence/T-1/full_suite.txt": "3 passed\n"},
    )
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.status == ef.STATUS_NOT_APPLICABLE
    assert "no stamped evidence" in result.evidence


# ------------------------------------------------------------------ HEAD moved for other reasons


def _stamped_then(repo: Path, files: dict[str, str], subject: str, body: str | None = None) -> str:
    """Create evidence stamped at a known commit, then one more commit on top."""
    stamped = _commit(repo, "evidence", {"verify-evidence/T-1/full_suite.txt": "# HEAD=PARENT\n"})
    stamped = _commit(
        repo,
        "evidence re-stamped",
        {"verify-evidence/T-1/full_suite.txt": f"# HEAD={stamped}\n3 passed\n"},
    )
    return _commit(repo, subject, files, body=body)


def test_a_recovery_commit_after_the_stamp_is_explained_not_called_stale(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    recovered = _stamped_then(
        repo,
        {"scratch.txt": "leftover\n"},
        "chore(orchestrator-recovery): sweep",
        body="Orchestrator-Recovery: R-1",
    )
    result = ef.check_freshness(repo, recovered, "T-1", "main")
    assert result.status == ef.STATUS_PASS
    assert "harness recovery commit" in result.notes


def test_an_operator_hand_fix_after_the_stamp_is_explained_not_called_stale(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fixed = _stamped_then(
        repo,
        {"src/app.py": "x = 1\n"},
        "fix(phase-1): operator repair",
        body="Operator-Hand-Fix: chat-10",
    )
    result = ef.check_freshness(repo, fixed, "T-1", "main")
    assert result.status == ef.STATUS_PASS
    assert "operator hand-fix commit" in result.notes


def test_an_evidence_only_commit_after_the_stamp_is_explained_not_called_stale(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    stamped = _commit(repo, "evidence", {"verify-evidence/T-1/full_suite.txt": "# HEAD=PARENT\n"})
    stamped = _commit(
        repo,
        "evidence re-stamped",
        {"verify-evidence/T-1/full_suite.txt": f"# HEAD={stamped}\n3 passed\n"},
    )
    later = _commit(
        repo,
        "test(phase-1): more evidence",
        {"verify-evidence/T-1/lint.txt": f"# HEAD={stamped}\nAll checks passed\n"},
    )
    result = ef.check_freshness(repo, later, "T-1", "main")
    assert result.status == ef.STATUS_PASS
    assert "evidence-only commit" in result.notes


def test_a_code_commit_after_the_stamp_is_stale(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = _stamped_then(repo, {"src/app.py": "x = 2\n"}, "fix(phase-1): change the code")
    result = ef.check_freshness(repo, code, "T-1", "main")
    assert result.status == ef.STATUS_STALE
    assert result.stale_paths == ["verify-evidence/T-1/full_suite.txt"]
    assert ef._short(repo, code) in result.notes


# ------------------------------------------------------------------ deletion is a finding


def test_deleted_evidence_names_the_paths_and_the_deleting_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _commit(
        repo,
        "evidence",
        {
            "verify-evidence/T-1/full_suite.txt": "# HEAD=PARENT\n3 passed\n",
            "verify-evidence/T-1/README.md": "# evidence\n",
        },
    )
    _commit(
        repo,
        "evidence re-stamped",
        {
            "verify-evidence/T-1/full_suite.txt": f"# HEAD={first}\n3 passed\n",
            "verify-evidence/T-1/README.md": "# evidence\n",
        },
    )
    (repo / "verify-evidence/T-1/README.md").unlink()
    deleted = _commit(repo, "test(phase-1): delete README evidence", {})
    result = ef.check_freshness(repo, deleted, "T-1", None)
    assert result.status == ef.STATUS_STALE
    assert result.deleted_paths == ["verify-evidence/T-1/README.md"]
    assert result.deleted_by["verify-evidence/T-1/README.md"] == deleted
    assert "deleted by" in result.notes


# ------------------------------------------------------------------ the single generator


CAPTURE = """#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
OUT="$ROOT/verify-evidence/T-1"
SHA="$(git rev-parse HEAD)"
printf '# HEAD=%s\\n' "$SHA" > "$OUT/full_suite.txt"
printf 'suite: 3 passed\\n' >> "$OUT/full_suite.txt"
printf '# HEAD=%s\\n# HEAD=%s\\n' "$SHA" "$SHA" > "$OUT/two_stamps.txt"
"""


def _bundle_repo(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    evidence = repo / "verify-evidence/T-1"
    evidence.mkdir(parents=True)
    script = evidence / "capture.sh"
    script.write_text(CAPTURE)
    script.chmod(0o755)
    _commit(repo, "add evidence generator", {"verify-evidence/T-1/capture.sh": CAPTURE})
    script.chmod(0o755)
    return repo


def test_regeneration_is_one_command_and_second_run_is_a_no_op(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    first = ef.regenerate_bundle(repo, "T-1")
    assert first.head == _git(repo, "rev-parse", "HEAD").strip()
    assert set(first.files) == {"capture.sh", "full_suite.txt", "two_stamps.txt"}
    assert first.manifest.endswith("verify-evidence/T-1/manifest.json")
    assert "manifest.json" in first.rewritten or "two_stamps.txt" in first.rewritten

    second = ef.regenerate_bundle(repo, "T-1")
    assert second.rewritten == []
    assert second.generated_at == first.generated_at


def test_the_generator_writes_the_manifest_fields_and_one_canonical_stamp(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    result = ef.regenerate_bundle(repo, "T-1")
    manifest = json.loads((repo / "verify-evidence/T-1/manifest.json").read_text())
    assert manifest["head"] == result.head
    assert manifest["command"] == "verify-evidence/T-1/capture.sh"
    assert manifest["generated_at"].endswith("Z")
    assert manifest["tool_versions"]["python"]
    assert manifest["tool_versions"]["git"]
    assert {entry["path"] for entry in manifest["files"]} == {
        "capture.sh",
        "full_suite.txt",
        "two_stamps.txt",
    }
    text = (repo / "verify-evidence/T-1/two_stamps.txt").read_text()
    assert text.count("# HEAD=") == 1
    assert text.startswith("# HEAD=" + result.head + "\n")


def test_a_missing_generator_is_an_error_not_a_silent_stamp_refresh(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1")
    assert "no committed generator" in str(exc.value)


def test_the_generated_bundle_is_reported_fresh_by_the_check(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "evidence")
    head = _git(repo, "rev-parse", "HEAD").strip()
    # The stamps name the parent (the commit the generator ran at); committing the generated
    # files is an evidence-only commit, so the bundle is still fresh at the tip.
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.status == ef.STATUS_PASS
    assert result.stamped_files == 3  # full_suite, two_stamps and manifest all carry HEAD
