"""T-15: committed evidence freshness is a deterministic equality test, not a reviewer's reading.

Offline, local git repositories only: every case builds its own history with real stamps, so the
checks run against the shapes the repository actually carries (legacy stamp formats, recovery
commits, operator hand-fixes, evidence-only commits, short SHAs, deleted bundles).

The generator cases exist because an earlier round let `--no-capture` re-stamp old content at the
current HEAD, which turned a stale bundle into `pass` with one command. A pass here means the
bytes were regenerated, not that a stamp was moved.
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
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV
    ).stdout


def _commit(repo: Path, subject: str, files: dict[str, str], body: str | None = None) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    args = ["commit", "-q", "-m", subject] + (["-m", body] if body else [])
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD").strip()


def _repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo", {"README.md": "# test\n"})


def _stamp_file(sha: str, text: str = "suite: 3 passed\n") -> dict[str, str]:
    return {"verify-evidence/T-1/full_suite.txt": f"# HEAD={sha}\n{text}"}


# ------------------------------------------------------------------ the legacy stamp vocabulary

FULL = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("line", "sha"),
    [
        (f"# HEAD={FULL}", FULL),
        (f"# HEAD={FULL} ({FULL[:7]}) branch=task-4", FULL),
        (f"# HEAD {FULL}", FULL),
        (f"# HEAD: {FULL}", FULL),                        # the T-9 shape
        ("# T-18 full suite at HEAD 0123456", "0123456"),  # prose, but in a comment
        ("# HEAD=0123456 tracked-dirty-entries=0", "0123456"),
        (f"HEAD {FULL} | dirty-entries 0 | 2026-11-08T00:00:00Z", FULL),  # the T-20 shape
        (f"# HEAD={FULL} tracked-and-untracked-entries-outside-this-dir=0", FULL),
        ("== commit under test: task-6 @ 0123456 ==", "0123456"),  # the T-6 shape
    ],
)
def test_every_legacy_stamp_shape_is_parsed(line: str, sha: str) -> None:
    stamps, unparsed = ef.parse_stamps(line + "\nbody\n")
    assert [stamp.sha for stamp in stamps] == [sha]
    assert unparsed == []


def test_prose_and_a_key_value_table_row_are_neither_stamps_nor_unparsable() -> None:
    text = (
        "# Run from the repo root at the code HEAD you want to certify.\n"
        "   HEAD tree  : 751117fcb41ce51b820ed208fa29104a4bedc469\n"
    )
    stamps, unparsed = ef.parse_stamps(text)
    assert stamps == []
    assert unparsed == []  # reported as an unparsable stamp only for `HEAD=`, `HEAD:` markers


def test_a_stamp_mentioned_inside_captured_output_is_not_a_stamp() -> None:
    """The parametrized ids in this very repo embed the stamp shapes with a fake sha."""
    mention = "tests/unit/test_x.py::test_y[# HEAD=0123456789abcdef-0123456789abcdef] PASSED"
    stamps, unparsed = ef.parse_stamps(mention + "\n")
    assert stamps == []
    assert unparsed == []


def test_a_stamp_below_the_header_window_is_neither_counted_nor_rewritten(tmp_path: Path) -> None:
    """Stamps are header metadata; a transcript body must not stale or be rewritten by one."""
    body = "\n".join(f"line {i}" for i in range(30))
    text = f"# HEAD={FULL}\n{body}\n# HEAD=deadbeef\n"
    stamps, _unparsed = ef.parse_stamps(text)
    assert [stamp.sha for stamp in stamps] == [FULL]
    path = tmp_path / "log.txt"
    path.write_text(text)
    assert ef.canonicalize_stamps(path, "cafe123") is True
    written = path.read_text()
    assert written.startswith("# HEAD=cafe123 mode=fast\n")
    assert "# HEAD=deadbeef" in written  # the deep line is captured output, not a stamp


def test_an_unparsable_stamp_is_reported_not_treated_as_fresh(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _commit(repo, "evidence", _stamp_file("not-a-sha"))
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.unparsed_paths == ["verify-evidence/T-1/full_suite.txt"]
    assert "unparsable HEAD stamp" in result.notes


def test_a_short_sha_is_prefix_resolved_against_the_commit_under_verification(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    head = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    short = ef._short(repo, head)
    ok, reason = ef._stamp_verdict(repo, ef.EvidenceStamp(short, f"# HEAD={short}", 1), head)
    assert ok is True
    assert reason == "matches the commit under verification"


# ------------------------------------------------------------------ pass / stale / not_applicable


def test_an_older_stamp_is_stale_with_the_paths_and_the_count(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old = _commit(repo, "round 4 evidence", {"verify-evidence/T-1/old.txt": "old\n"})
    _commit(
        repo,
        "evidence stamped at the round-4 tip",
        {
            "verify-evidence/T-1/full_suite.txt": f"# HEAD={old}\n3 passed\n",
            "verify-evidence/T-1/tests.txt": f"# HEAD={old}\n2 passed\n",
        },
    )
    new = _commit(repo, "round 5 code", {"src/app.py": "def handle():\n    return 1\n"})
    result = ef.check_freshness(repo, new, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.stale_paths == [
        "verify-evidence/T-1/full_suite.txt",
        "verify-evidence/T-1/tests.txt",
    ]
    assert result.stale_count == 2
    assert ef._short(repo, new) in result.evidence
    assert "non-evidence commit" in result.notes


def test_a_stamp_that_differs_only_by_an_evidence_only_commit_passes(tmp_path: Path) -> None:
    """A bundle delivered by an evidence-only commit passes: the code HEAD is unchanged."""
    repo = _repo(tmp_path)
    first = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    head = _commit(repo, "evidence", _stamp_file(first))
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_PASS
    assert result.compared_commit == first  # the code HEAD the stamp names
    assert result.commit_under_verification == head  # the evidence-only tip
    assert ef._short(repo, first) in result.evidence  # the reason names the target


def test_absent_evidence_is_not_applicable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = ef.check_freshness(repo, _commit(repo, "code", {"src/app.py": "x"}), "T-1", "main")
    assert result.state == ef.STATE_NOT_APPLICABLE
    assert "no committed evidence" in result.evidence


def test_unstamped_evidence_is_not_applicable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _commit(repo, "evidence", {"verify-evidence/T-1/notes.txt": "3 passed\n"})
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_NOT_APPLICABLE
    assert "no stamped evidence" in result.evidence


def test_the_check_status_uses_the_approved_stale_value() -> None:
    """CCR-T15-01 (approved 2026-11-08): a compared-and-out-of-date bundle is `stale`.

    One assertion per outcome: the fifth value, the strict override, and the two unchanged values.
    """
    stale = ef.FreshnessResult(ef.STATE_STALE, "1 stale of 1")
    assert ef.check_status(stale) == "stale"
    assert ef.check_status(stale, strict=True) == "fail"
    assert ef.check_status(ef.FreshnessResult(ef.STATE_PASS)) == "pass"
    assert ef.check_status(ef.FreshnessResult(ef.STATE_NOT_APPLICABLE)) == "not_applicable"


def test_the_stale_row_renders_into_verification_md() -> None:
    """The renderer is a consumer of the new value: it must emit `stale`, not fall back."""
    from orchestrator.core.schemas import VerificationCheck, VerificationResult
    from orchestrator.services import artifact_service

    result = VerificationResult(
        phase="phase-15",
        final_result="pass",
        checks=[
            VerificationCheck(
                name=ef.CHECK_NAME,
                status="stale",
                evidence="1 stale of 1 stamped evidence file(s) against abc1234 (abcdef1)",
                notes="verify-evidence/T-1/full_suite.txt: stamp predates commit",
            )
        ],
    )
    md = artifact_service.render_verification_md(result)
    assert f"| {ef.CHECK_NAME} | stale |" in md
    assert "1 stale of 1 stamped evidence file(s)" in md


def test_the_freshness_scope_is_the_two_code_writing_kinds() -> None:
    """One predicate for both call sites: agent/review tasks get neither row nor finding."""
    assert ef.applies_to_kind("coding") is True
    assert ef.applies_to_kind("maintenance") is True
    assert ef.applies_to_kind("agent") is False
    assert ef.applies_to_kind("review") is False


# ------------------------------------------------------------------ HEAD moved for other reasons


def _stamped_then(repo: Path, files: dict[str, str], subject: str, body: str | None = None) -> str:
    """A bundle stamped at a real commit, then one more commit on top of it."""
    stamped = _commit(repo, "evidence", {"verify-evidence/T-1/full_suite.txt": "# HEAD=PARENT\n"})
    stamped = _commit(repo, "evidence re-stamped", _stamp_file(stamped))
    return _commit(repo, subject, files, body=body)


def test_a_recovery_trailer_is_explained_not_certified(tmp_path: Path) -> None:
    """A trailer explains the move; it cannot certify content a Worker could have written."""
    repo = _repo(tmp_path)
    head = _stamped_then(
        repo, {"scratch.txt": "leftover\n"}, "chore(recovery): sweep",
        body="Orchestrator-Recovery: R-1",
    )
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.stale_paths == ["verify-evidence/T-1/full_suite.txt"]
    assert "harness recovery commit (explained, not certified)" in result.notes
    assert ef._short(repo, head) in result.notes


def test_an_operator_hand_fix_trailer_is_explained_not_certified(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _stamped_then(
        repo, {"src/app.py": "x = 1\n"}, "fix(phase-1): operator repair",
        body="Operator-Hand-Fix: chat-10",
    )
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.stale_paths == ["verify-evidence/T-1/full_suite.txt"]
    assert "operator hand-fix commit (explained, not certified)" in result.notes


def test_a_worker_can_fake_the_hand_fix_trailer_and_still_reads_stale(tmp_path: Path) -> None:
    """The security case: the trailer is not a certificate the Worker can grant itself."""
    repo = _repo(tmp_path)
    head = _stamped_then(
        repo, {"src/app.py": "x = 2\n"}, "fix(phase-1): not really an operator",
        body="Operator-Hand-Fix: chat-99",
    )
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.stale_count == 1
    assert "does not certify" in result.notes


def test_a_body_that_merely_mentions_the_trailer_is_not_exempt(tmp_path: Path) -> None:
    """Trailers are parsed by git, not found by substring: prose is not an exemption."""
    repo = _repo(tmp_path)
    head = _stamped_then(
        repo, {"src/app.py": "x = 2\n"}, "fix(phase-1): worker changed the code"
    )
    _git(repo, "commit", "-q", "--amend", "-m",
         "fix(phase-1): worker changed the code\n\n"
         "A paragraph that says Operator-Hand-Fix: chat-3 while describing the protocol.\n")
    amended = _git(repo, "rev-parse", "HEAD").strip()
    assert amended != head
    result = ef.check_freshness(repo, amended, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.stale_paths == ["verify-evidence/T-1/full_suite.txt"]


def test_a_commit_that_only_touches_the_generator_is_not_evidence_only(tmp_path: Path) -> None:
    """Editing capture.sh changes what every number means, so it is a code commit."""
    repo = _repo(tmp_path)
    head = _stamped_then(
        repo,
        {"verify-evidence/T-1/capture.sh": "#!/usr/bin/env bash\necho changed\n"},
        "fix(phase-1): change what the capture measures",
    )
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_STALE


def test_a_code_commit_after_the_stamp_is_stale(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _stamped_then(repo, {"src/app.py": "x = 2\n"}, "fix(phase-1): change the code")
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert ef._short(repo, head) in result.notes


# ------------------------------------------------------------------ ruling (d): the walk-back target


def test_a_bundle_on_top_of_code_passes_and_names_the_code_head(tmp_path: Path) -> None:
    """(i) Bundle on code X, tip = evidence-only commit(s): pass, and the reason names X."""
    repo = _repo(tmp_path)
    code = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    _commit(repo, "evidence", _stamp_file(code))
    tip = _commit(repo, "more evidence", {"verify-evidence/T-1/notes.txt": "done\n"})
    result = ef.check_freshness(repo, tip, "T-1", "main")
    assert result.state == ef.STATE_PASS
    assert result.compared_commit == code
    assert result.commit_under_verification == tip
    assert ef._short(repo, code) in result.evidence


def test_a_code_commit_after_the_bundle_is_stale_and_names_both_heads(tmp_path: Path) -> None:
    """(ii) A code commit after the bundle is stale, naming the stamp and the newer code commit."""
    repo = _repo(tmp_path)
    code = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    _commit(repo, "evidence", _stamp_file(code))
    newer = _commit(repo, "fix(phase-1): code moves on", {"src/app.py": "x = 2\n"})
    result = ef.check_freshness(repo, newer, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.compared_commit == newer
    assert ef._short(repo, code) in result.notes
    assert ef._short(repo, newer) in result.notes


def test_a_recovery_commit_after_the_bundle_is_stale_with_the_trailer_explanation(
    tmp_path: Path,
) -> None:
    """(iii) A recovery commit that swept SOURCE changes code: target, stale, trailer explained."""
    repo = _repo(tmp_path)
    code = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    _commit(repo, "evidence", _stamp_file(code))
    recovery = _commit(
        repo,
        "chore(orchestrator-recovery): sweep",
        {"src/app.py": "x = 2\n"},
        body="Orchestrator-Recovery: R-1",
    )
    result = ef.check_freshness(repo, recovery, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.compared_commit == recovery
    assert "harness recovery commit (explained, not certified)" in result.notes


def test_an_evidence_only_commit_after_a_stale_bundle_does_not_certify_it(tmp_path: Path) -> None:
    """(iv) Evidence-only commits are walked past; they never turn stale into pass."""
    repo = _repo(tmp_path)
    code = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    _commit(repo, "evidence", _stamp_file(code))
    newer = _commit(repo, "fix(phase-1): code moves", {"src/app.py": "x = 2\n"})
    tip = _commit(
        repo, "evidence on top of the stale bundle", {"verify-evidence/T-1/notes.txt": "more\n"}
    )
    for commit in (newer, tip):
        result = ef.check_freshness(repo, commit, "T-1", "main")
        assert result.state == ef.STATE_STALE
        assert result.compared_commit == newer


def test_an_empty_commit_neither_certifies_nor_explains(tmp_path: Path) -> None:
    """(v) An empty commit is walked past: it changes nothing, so it changes no verdict."""
    repo = _repo(tmp_path)
    code = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    _commit(repo, "evidence", _stamp_file(code))
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: empty")
    empty = _git(repo, "rev-parse", "HEAD").strip()
    fresh = ef.check_freshness(repo, empty, "T-1", "main")
    assert fresh.state == ef.STATE_PASS
    assert fresh.compared_commit == code

    newer = _commit(repo, "fix", {"src/app.py": "x = 2\n"})
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: empty again")
    tip = _git(repo, "rev-parse", "HEAD").strip()
    stale = ef.check_freshness(repo, tip, "T-1", "main")
    assert stale.state == ef.STATE_STALE
    assert stale.compared_commit == newer


# ------------------------------------------------------------------ deletion is its own finding


def test_deleted_evidence_names_the_paths_and_the_deleting_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    files = {
        "verify-evidence/T-1/full_suite.txt": "# HEAD=PARENT\n3 passed\n",
        "verify-evidence/T-1/README.md": "# evidence\n",
    }
    first = _commit(repo, "evidence", files)
    _commit(repo, "evidence re-stamped", {**_stamp_file(first), **_readme()})
    (repo / "verify-evidence/T-1/README.md").unlink()
    deleted = _commit(repo, "test(phase-1): delete README evidence", {})
    result = ef.check_freshness(repo, deleted, "T-1", None)
    assert result.state == ef.STATE_STALE
    assert result.deleted_paths == ["verify-evidence/T-1/README.md"]
    assert result.deleted_by["verify-evidence/T-1/README.md"] == deleted
    assert "deleted by" in result.notes


def _readme() -> dict[str, str]:
    return {"verify-evidence/T-1/README.md": "# evidence\n"}


# ------------------------------------------------------------------ the single generator

CAPTURE = """#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
OUT="$ROOT/verify-evidence/T-1"
SHA="$(git rev-parse HEAD)"
mkdir -p "$OUT"
printf '# HEAD=%s\\nsuite: 3 passed\\n' "$SHA" > "$OUT/suite.txt"
if [ ! -f "$OUT/slow.txt" ]; then printf '# HEAD=%s\\nslow: 1 passed\\n' "$SHA" > "$OUT/slow.txt"; fi
"""


def _bundle_repo(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    script = repo / "verify-evidence/T-1/capture.sh"
    script.parent.mkdir(parents=True)
    script.write_text(CAPTURE)
    script.chmod(0o755)
    _commit(repo, "add the evidence generator", {"verify-evidence/T-1/capture.sh": CAPTURE})
    return repo


def _commit_all(repo: Path, subject: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


def test_regeneration_is_one_command_and_a_second_run_rewrites_nothing(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    first = ef.regenerate_bundle(repo, "T-1")
    assert first.head == _git(repo, "rev-parse", "HEAD").strip()
    assert set(first.files) == {"capture.sh", "suite.txt", "slow.txt", ef.SELF_CHECK_NAME}
    assert Path(first.manifest).name == ef.MANIFEST_NAME
    assert {"suite.txt", "slow.txt"} <= set(first.rewritten)
    second = ef.regenerate_bundle(repo, "T-1")
    assert second.rewritten == []
    assert second.generated_at == first.generated_at


def test_the_manifest_names_the_head_command_versions_and_files(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    result = ef.regenerate_bundle(repo, "T-1")
    manifest = json.loads((repo / "verify-evidence/T-1" / ef.MANIFEST_NAME).read_text())
    assert manifest["head"] == result.head
    assert manifest["command"] == "verify-evidence/T-1/capture.sh"
    assert manifest["generated_at"].endswith("Z")
    assert manifest["tool_versions"]["python"] and manifest["tool_versions"]["git"]
    assert manifest["stamp"] == f"# HEAD={result.head} mode=fast"
    assert manifest["mode"] == "fast"
    # `files` is the list of files PRODUCED; the generator is an input and is declared separately,
    # so `--only capture.sh` can never be accepted as a declared output.
    assert {entry["path"] for entry in manifest["files"]} == {
        "suite.txt",
        "slow.txt",
        ef.SELF_CHECK_NAME,
    }
    assert manifest["generator"] == "verify-evidence/T-1/capture.sh"
    assert manifest["inputs"] == ["capture.sh"]
    assert "# HEAD=" not in (repo / "verify-evidence/T-1/suite.txt").read_text().split("\n", 1)[1]


def test_regeneration_does_not_restamp_a_file_the_generator_did_not_rewrite(
    tmp_path: Path,
) -> None:
    """The stamp-only hole: old bytes re-stamped at a new HEAD would pass the check."""
    repo = _bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    _commit_all(repo, "test(phase-1): evidence-only commit")
    new = _commit(repo, "fix(phase-1): change the code", {"src/app.py": "x = 1\n"})
    result = ef.regenerate_bundle(repo, "T-1")
    assert "suite.txt" in result.rewritten
    assert "slow.txt" not in result.rewritten  # capture.sh deliberately left it alone here
    committed = ef.check_freshness(repo, new, "T-1", "main", from_worktree=True)
    assert committed.stale_paths == ["verify-evidence/T-1/slow.txt"]


def test_the_generated_bundle_is_fresh_once_committed(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    head = _commit_all(repo, "test(phase-1): evidence at the code HEAD")
    selfcheck = (repo / "verify-evidence/T-1" / ef.SELF_CHECK_NAME).read_text()
    assert "basis=worktree" in selfcheck
    assert "status=pass" in selfcheck
    result = ef.check_freshness(repo, head, "T-1", "main")
    assert result.state == ef.STATE_PASS
    assert result.stamped_files == 3  # suite.txt, slow.txt and the manifest each carry a HEAD
    assert result.stale_paths == []


def test_a_dirty_tracked_worktree_is_refused_rather_than_stamped(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    (repo / "README.md").write_text("# edited but not committed\n")
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1")
    assert "uncommitted inputs" in str(exc.value)
    assert "README.md" in str(exc.value)
    assert not (repo / "verify-evidence/T-1/suite.txt").exists()


def test_an_untracked_source_file_is_refused_because_the_stamp_would_certify_it(
    tmp_path: Path,
) -> None:
    """`capture.sh` measures the tree on disk; an untracked file is code the commit lacks."""
    repo = _bundle_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "untracked.py").write_text("x = 1\n")
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1")
    assert "untracked" in str(exc.value)
    assert "src/untracked.py" in str(exc.value)
    assert not (repo / "verify-evidence/T-1/suite.txt").exists()


def test_no_arbitrary_command_is_accepted_because_only_the_committed_generator_runs() -> None:
    """A command the branch does not contain can re-stamp content it never measured."""
    import inspect

    from orchestrator import cli

    assert "command" not in inspect.signature(ef.regenerate_bundle).parameters
    assert "only" in inspect.signature(ef.regenerate_bundle).parameters
    assert "command" not in inspect.signature(cli.task_evidence).parameters


def test_an_edited_generator_is_refused_even_though_it_lives_in_the_evidence_dir(
    tmp_path: Path,
) -> None:
    """The generator is code: an uncommitted edit must not be stamped as if it were committed."""
    repo = _bundle_repo(tmp_path)
    (repo / "verify-evidence/T-1/capture.sh").write_text(CAPTURE + "\n# edited\n")
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1")
    assert "edited generator" in str(exc.value)
    assert not (repo / "verify-evidence/T-1/suite.txt").exists()


def test_a_generator_that_is_not_committed_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    script = repo / "verify-evidence/T-1/capture.sh"
    script.parent.mkdir(parents=True)
    script.write_text(CAPTURE)
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1")
    assert "is not committed" in str(exc.value)


def test_a_missing_generator_is_an_error_not_a_silent_stamp_refresh(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1")
    assert "no committed generator" in str(exc.value)


def test_the_full_pass_is_opt_in_and_recorded_in_the_manifest(tmp_path: Path) -> None:
    repo = _bundle_repo(tmp_path)
    result = ef.regenerate_bundle(repo, "T-1", full=True)
    manifest = json.loads((repo / "verify-evidence/T-1/manifest.json").read_text())
    assert result.mode == "full"
    assert manifest["mode"] == "full"
    assert manifest["stamp"].endswith("mode=full")


def test_a_manifest_declared_generator_helper_is_not_evidence_only(tmp_path: Path) -> None:
    """Ruling (d) finding 4: every declared generator/input counts, not one hardcoded filename."""
    repo = _repo(tmp_path)
    manifest = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh", "helper.py"],
            "files": [],
        }
    )
    _commit(repo, "bundle manifest", {"verify-evidence/T-1/manifest.json": manifest})
    helper = _commit(
        repo, "change the helper generator", {"verify-evidence/T-1/helper.py": "print(1)\n"}
    )
    walkable, reason = ef._commit_class(repo, helper)
    assert walkable is False
    assert reason is None


def test_only_refuses_a_generator_input_by_name(tmp_path: Path) -> None:
    """Ruling (d) finding 7: `files` lists outputs only; an input is refused, never selected."""
    repo = _selective_bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    _commit_all(repo, "test(phase-1): evidence at the code HEAD")
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1", only=["capture.sh"])
    assert "generator/input, not an output" in str(exc.value)


def test_an_evidence_directory_that_lexically_escapes_is_refused(tmp_path: Path) -> None:
    """Ruling (d) finding 8: resolve both sides before `is_relative_to`."""
    repo = _bundle_repo(tmp_path)
    escape = repo / "verify-evidence" / ".." / ".." / "escape"
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1", directory=str(escape))
    assert "outside" in str(exc.value)


def test_the_default_evidence_path_is_resolved_too(tmp_path: Path) -> None:
    """Ruling (e) item 3: the CLI uses the default path, so a `..` label must be refused there.

    The explicit `directory` argument was the only side resolved, so the containment check stayed
    lexical for exactly the path operators use.
    """
    repo = _bundle_repo(tmp_path)
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "../../escape")
    assert "outside" in str(exc.value)


def test_a_generator_edit_cannot_certify_itself_by_dropping_its_declaration(
    tmp_path: Path,
) -> None:
    """Ruling (e) item 4: declared inputs are unioned with the parent(s).

    Without the union, one commit could edit a helper generator AND remove it from `inputs`, so
    the manifest at that commit no longer declares it, the commit classifies as evidence-only, the
    walk-back skips it, and the bundle reports pass on a generator change.
    """
    repo = _repo(tmp_path)
    declares_helper = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh", "helper.py"],
            "files": [],
        }
    )
    code = _commit(
        repo,
        "code plus a bundle that declares its helper",
        {
            "src/app.py": "x = 1\n",
            "verify-evidence/T-1/manifest.json": declares_helper,
            "verify-evidence/T-1/helper.py": "print(0)\n",
        },
    )
    # A genuine bundle commit on top is evidence-only and walked past, so the target is `code`.
    _commit(
        repo,
        "bundle stamped at the code HEAD",
        {"verify-evidence/T-1/full_suite.txt": f"# HEAD={code}\n3 passed\n"},
    )
    drops_helper = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh"],
            "files": [],
        }
    )
    commit = _commit(
        repo,
        "change the helper and drop the declaration in the same commit",
        {
            "verify-evidence/T-1/helper.py": "print(1)\n",
            "verify-evidence/T-1/manifest.json": drops_helper,
        },
    )
    walkable, reason = ef._commit_class(repo, commit)
    assert walkable is False
    assert reason is None
    result = ef.check_freshness(repo, commit, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.compared_commit == commit


def test_an_evidence_only_commit_is_walkable_when_the_parent_declares_a_generator(
    tmp_path: Path,
) -> None:
    """(iii) The parent union does not block a genuine evidence-only commit."""
    repo = _repo(tmp_path)
    # A real manifest lists what the pass produced (NOT_MEASURED excepted), so the fixture must
    # too: under the ruling (f) whitelist a path under the evidence root that no manifest declares
    # as an output is treated as code.
    manifest = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh", "helper.py"],
            "files": [{"path": "notes.txt", "sha256": "0" * 64}],
        }
    )
    _commit(
        repo,
        "bundle declares a helper",
        {
            "verify-evidence/T-1/manifest.json": manifest,
            "verify-evidence/T-1/helper.py": "print(0)\n",
        },
    )
    commit = _commit(repo, "add notes only", {"verify-evidence/T-1/notes.txt": "done\n"})
    assert ef._commit_class(repo, commit) == (True, "evidence-only commit")


def test_dropping_an_untouched_generator_declaration_stays_evidence_only(tmp_path: Path) -> None:
    """(iv) A child that never touches the helper is not blocked by the parent's declaration."""
    repo = _repo(tmp_path)
    manifest = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh", "helper.py"],
            "files": [{"path": "notes.txt", "sha256": "0" * 64}],
        }
    )
    _commit(
        repo,
        "bundle declares a helper",
        {
            "verify-evidence/T-1/manifest.json": manifest,
            "verify-evidence/T-1/helper.py": "print(0)\n",
        },
    )
    rewritten = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh"],
            "files": [{"path": "notes.txt", "sha256": "0" * 64}],
        }
    )
    commit = _commit(
        repo,
        "add notes and re-declare inputs without touching the helper",
        {
            "verify-evidence/T-1/notes.txt": "done\n",
            "verify-evidence/T-1/manifest.json": rewritten,
        },
    )
    assert ef._commit_class(repo, commit) == (True, "evidence-only commit")


def test_a_merge_commit_never_certifies_a_stamp(tmp_path: Path) -> None:
    """`diff-tree` on a merge shows no files; it must not read as an empty, certifying commit."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "side code", {"src/side.py": "x = 1\n"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main code", {"src/main.py": "y = 1\n"})
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side")
    merge = _git(repo, "rev-parse", "HEAD").strip()
    certifies, reason = ef._commit_class(repo, merge)
    assert certifies is False
    assert reason == "merge commit (explained, not certified)"


# ------------------------------------------------------------------ --only, timing and the tip


SELECTIVE = """#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
OUT="$ROOT/verify-evidence/T-1"
SHA="$(git rev-parse HEAD)"
ONLY="${T15_ONLY:-}"
want() {
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}
mkdir -p "$OUT"
if want suite.txt; then
  printf 'T15_STEP|suite|7\\n'
  printf '# HEAD=%s\\nsuite: 3 passed\\n' "$SHA" > "$OUT/suite.txt"
fi
if want slow.txt; then
  printf 'T15_STEP|slow|11\\n'
  printf '# HEAD=%s\\nslow: 1 passed\\n' "$SHA" > "$OUT/slow.txt"
fi
"""


def _selective_bundle_repo(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    script = repo / "verify-evidence/T-1/capture.sh"
    script.parent.mkdir(parents=True)
    script.write_text(SELECTIVE)
    script.chmod(0o755)
    _commit(repo, "add the evidence generator", {"verify-evidence/T-1/capture.sh": SELECTIVE})
    return repo


def test_editing_the_hand_maintained_index_does_not_dirty_the_bundle(tmp_path: Path) -> None:
    """Prose a human maintains is not a measurement: no false hash, no rewrite, no restamp."""
    repo = _selective_bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    manifest = json.loads((repo / "verify-evidence/T-1" / ef.MANIFEST_NAME).read_text())
    assert ef.INDEX_NAME not in {entry["path"] for entry in manifest["files"]}
    (repo / "verify-evidence/T-1" / ef.INDEX_NAME).write_text("# index\n\nedited prose\n")
    second = ef.regenerate_bundle(repo, "T-1")
    assert second.rewritten == []
    assert "# index" not in (repo / "verify-evidence/T-1/suite.txt").read_text()


def test_only_refreshes_the_named_output_and_refuses_an_unknown_name(tmp_path: Path) -> None:
    """A subset refresh reselects a DECLARED output; an unknown name is refused, not guessed."""
    repo = _selective_bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    _commit_all(repo, "test(phase-1): evidence at the code HEAD")
    _commit(repo, "fix(phase-1): change the code", {"src/app.py": "x = 1\n"})

    result = ef.regenerate_bundle(repo, "T-1", only=["slow.txt"])
    assert result.only == ["slow.txt"]
    assert "slow.txt" in result.rewritten
    assert "suite.txt" not in result.rewritten
    fresh = ef.check_freshness(repo, "HEAD", "T-1", "main", from_worktree=True)
    assert fresh.stale_paths == ["verify-evidence/T-1/suite.txt"]

    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1", only=["not-a-real-file.txt"])
    assert "does not declare as an output" in str(exc.value)
    assert "slow.txt" in str(exc.value)  # the refusal lists the names it does recognise


def test_only_refuses_before_there_is_a_committed_manifest_to_select_from(tmp_path: Path) -> None:
    """Without a committed manifest there is no declaration of what the generator produces."""
    repo = _selective_bundle_repo(tmp_path)
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "T-1", only=["slow.txt"])
    assert "run a full pass and commit it first" in str(exc.value)


def test_timing_txt_holds_both_pass_modes_with_wall_time_and_rewrites(tmp_path: Path) -> None:
    """DoD 3: one committed file, both modes, numbers that come from the passes themselves.

    `files`/`rewritten` count what the GENERATOR saw and rewrote in that pass; the driver's own
    bookkeeping (manifest.json, the self-check, this file) is written after the capture and is not
    counted, which is what keeps the numbers identical to the ones the command prints.
    """
    repo = _selective_bundle_repo(tmp_path)
    fast = ef.regenerate_bundle(repo, "T-1")
    _commit_all(repo, "test(phase-1): evidence at the code HEAD")
    _commit(repo, "fix(phase-1): change the code", {"src/app.py": "x = 1\n"})
    full = ef.regenerate_bundle(repo, "T-1", full=True)
    text = (repo / "verify-evidence/T-1/timing.txt").read_text()
    assert text.startswith(
        f"# HEAD={full.head} mode=full"
    )  # a canonical stamp, so the gate can read it
    # One measured source per row: each mode carries the HEAD its own pass measured.
    assert (
        f"mode=fast head={fast.head} wall_seconds={round(fast.duration_seconds, 2):.2f} "
        "files=3 rewritten=2"
    ) in text
    assert (
        f"mode=full head={full.head} wall_seconds={round(full.duration_seconds, 2):.2f} "
        "files=4 rewritten=2"
    ) in text
    assert "suite_ms=7" in text
    assert "slow_ms=11" in text


def test_a_fast_pass_leaves_timing_txt_untouched_so_a_second_run_is_a_no_op(
    tmp_path: Path,
) -> None:
    """The fast pass is the idempotent one; a measurement log must not dirty what it certifies."""
    repo = _selective_bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    ef.regenerate_bundle(repo, "T-1", full=True)
    before = (repo / "verify-evidence/T-1/timing.txt").read_text()
    ef.regenerate_bundle(repo, "T-1")
    assert (repo / "verify-evidence/T-1/timing.txt").read_text() == before


def test_the_at_tip_report_names_the_commit_it_compared(tmp_path: Path) -> None:
    """The committed-view proof at the delivered tip: names the commit, and is not a stamp."""
    repo = _selective_bundle_repo(tmp_path)
    code = _git(repo, "rev-parse", "HEAD").strip()
    ef.regenerate_bundle(repo, "T-1")
    head = _commit_all(repo, "test(phase-1): evidence at the code HEAD")
    result, path = ef.write_at_tip_report(repo, "T-1")
    text = path.read_text()
    assert result.state == ef.STATE_PASS
    # It prints BOTH: the delivered tip it ran at and the code HEAD it compared the stamps to.
    assert f"delivered_tip={head}" in text
    assert f"compared_commit={code}" in text
    assert "status=pass" in text
    # A report, not evidence measured at that commit: the parser reads no stamp in it.
    stamps, unparsed = ef.parse_stamps(text)
    assert stamps == []
    assert unparsed == []
    # An evidence-only commit adding the report is walked back to the same code HEAD: still fresh.
    _commit(repo, "test(phase-1): add the tip report", {str(path.relative_to(repo)): text})
    at_tip = ef.check_freshness(repo, "HEAD", "T-1", "main")
    assert at_tip.state == ef.STATE_PASS
    assert at_tip.compared_commit == code  # names the code HEAD, not the report commit
    assert ef._short(repo, code) in at_tip.evidence


def test_the_at_tip_report_refuses_to_claim_pass_on_a_stale_bundle(tmp_path: Path) -> None:
    repo = _selective_bundle_repo(tmp_path)
    ef.regenerate_bundle(repo, "T-1")
    _commit_all(repo, "test(phase-1): evidence at the code HEAD")
    _commit(repo, "fix(phase-1): code moves on", {"src/app.py": "x = 1\n"})
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.write_at_tip_report(repo, "T-1")
    assert "refusing to write freshness_at_tip.txt" in str(exc.value)
    assert not (repo / "verify-evidence/T-1" / ef.AT_TIP_NAME).exists()


# ------------------------------------------------------------------ DoD 4 shape rule


def _assertion_shape():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "t15_assertion_shape.py"
    spec = importlib.util.spec_from_file_location("t15_assertion_shape", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_memory_scope_tests_keep_their_assertion_shape(tmp_path: Path) -> None:
    """DoD 4's rule, not a count (T-17 touches these files next).

    Mutation-checked in the same process against a copy of the tree, never the committed files.
    """
    shape = _assertion_shape()
    assert shape.shape_failures() == []

    root = tmp_path / "shape"
    for name in shape.FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((shape.repo_root() / name).read_text())
    target = root / shape.FILES[0]
    target.write_text(
        target.read_text()
        + '\n\ndef test_mutated(session):\n    assert "rejected" in row["why"]\n'
    )
    failures = shape.shape_failures(root)
    assert len(failures) == 1
    assert failures[0].endswith('reason asserted by substring: "rejected" in row["why"]')


# ------------------------------------------------- ruling (f): whitelist outputs, not generators
def test_a_two_commit_split_cannot_make_a_generator_change_walkable(tmp_path: Path) -> None:
    """R-75 finding 1 / ruling (f) item 1: the parent+child declaration union is not enough.

    Commit A is manifest-only and drops `helper.py` from `inputs`; commit B then edits
    `helper.py`. Under the union rule both looked evidence-only -- A because a manifest is bundle
    bookkeeping, B because by then no manifest declared the helper -- so `_comparison_target`
    walked past both and a generator change certified itself as fresh. `helper.py` is a script and
    no manifest declares it as an OUTPUT, so B stops the walk and the bundle reads stale.
    """
    repo = _repo(tmp_path)
    code = _commit(repo, "code", {"src/app.py": "x = 1\n"})
    declared = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh", "helper.py"],
            "files": [{"path": "full_suite.txt", "sha256": "0" * 64}],
        }
    )
    _commit(
        repo,
        "bundle",
        {
            **_stamp_file(code),
            "verify-evidence/T-1/manifest.json": declared,
            "verify-evidence/T-1/helper.py": "print(0)\n",
        },
    )
    undeclared = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh"],
            "files": [{"path": "full_suite.txt", "sha256": "0" * 64}],
        }
    )
    split = _commit(
        repo,
        "A: manifest only, drop the helper declaration",
        {"verify-evidence/T-1/manifest.json": undeclared},
    )
    edited = _commit(
        repo, "B: edit the helper", {"verify-evidence/T-1/helper.py": "print(1)\n"}
    )

    assert ef._commit_class(repo, split) == (True, "evidence-only commit")  # A alone is bookkeeping
    assert ef._commit_class(repo, edited) == (False, None)  # B is code
    result = ef.check_freshness(repo, edited, "T-1", "main")
    assert result.state == ef.STATE_STALE
    assert result.compared_commit == edited
    assert result.stale_paths == ["verify-evidence/T-1/full_suite.txt"]
    # The reason names both the stamp's commit and the comparison target it was measured against.
    joined = result.evidence + result.notes + " ".join(result.stale_reasons.values())
    assert ef._short(repo, code) in joined
    assert ef._short(repo, edited) in joined


def test_a_script_is_code_even_when_the_manifest_declares_it_as_an_output(tmp_path: Path) -> None:
    """Ruling (f) item 1: no declaration can turn an executable into a measurement.

    Without the suffix rule, declaring the helper under `files` would whitelist it and re-open the
    self-certification hole through the other door.
    """
    repo = _repo(tmp_path)
    manifest = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh"],
            "files": [{"path": "helper.py", "sha256": "0" * 64}],
        }
    )
    _commit(
        repo,
        "bundle declares the helper as an output",
        {
            "verify-evidence/T-1/manifest.json": manifest,
            "verify-evidence/T-1/helper.py": "print(0)\n",
        },
    )
    commit = _commit(
        repo, "edit the declared helper", {"verify-evidence/T-1/helper.py": "print(1)\n"}
    )
    assert ef._commit_class(repo, commit) == (False, None)


def test_a_bundle_commit_touching_outputs_manifest_and_index_is_walkable(tmp_path: Path) -> None:
    """The whitelist must not break the ordinary case it exists for.

    A real bundle commit writes the declared outputs plus the three NOT_MEASURED bookkeeping files
    (the manifest cannot hash itself, the index is prose, the at-tip report is written last). If
    those were not allowed, every bundle would report stale on the commit that ships it.
    """
    repo = _repo(tmp_path)
    _commit(repo, "generator", {"verify-evidence/T-1/capture.sh": CAPTURE})
    manifest = json.dumps(
        {
            "generator": "verify-evidence/T-1/capture.sh",
            "inputs": ["capture.sh"],
            "files": [
                {"path": "full_suite.txt", "sha256": "0" * 64},
                {"path": "lint.txt", "sha256": "1" * 64},
            ],
        }
    )
    commit = _commit(
        repo,
        "bundle",
        {
            "verify-evidence/T-1/manifest.json": manifest,
            "verify-evidence/T-1/full_suite.txt": "# HEAD=x\nsuite: 3 passed\n",
            "verify-evidence/T-1/lint.txt": "# HEAD=x\nAll checks passed!\n",
            "verify-evidence/T-1/README.md": "# index\n",
            "verify-evidence/T-1/freshness_at_tip.txt": "delivered_tip=x\ncompared=x\n",
        },
    )
    assert ef._commit_class(repo, commit) == (True, "evidence-only commit")


def test_regenerate_bundle_refuses_a_label_that_escapes_the_evidence_root(tmp_path: Path) -> None:
    """Ruling (f) item 2: containment is the EVIDENCE root, not the worktree root.

    `../src` resolves inside the worktree, so the old `is_relative_to(root)` check waved it through
    and `src` became the exempt `rel_dir` for the dirty guard and for canonicalisation -- the
    bundle would have stamped, and excused uncommitted dirt in, the very source tree it certifies.
    """
    repo = _bundle_repo(tmp_path)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "app.py").write_text("x = 1\n")
    with pytest.raises(ef.EvidenceBundleError) as exc:
        ef.regenerate_bundle(repo, "../src")
    assert "evidence root" in str(exc.value)
    assert not (repo / "src" / "suite.txt").exists()
