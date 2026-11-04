#!/usr/bin/env python3
"""Print the cited T-10 code, compactly, from THIS checkout (merge-base 359a991).

The review payload truncates a multi-thousand-line diff from the middle, so a handoff that
says "see run_service.py:461" cannot be checked from the diff alone. This emits only the
line windows and anchors each DoD item cites, plus the commit list and diffstat, stamped with
the HEAD it ran at. Output is committed as `zz_code_map.txt`; the `zz_` prefix makes it sort
LAST so it survives the tail of a truncated diff.

Run: python3 verify-evidence/T-10/collect_code_map.py > verify-evidence/T-10/zz_code_map.txt
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (label, path, symbol, max body lines to show)
FUNCTIONS = [
    ("DoD 3 fingerprint", "src/orchestrator/services/run_service.py", "handoff_snapshot", 20),
    ("DoD 1/5 read", "src/orchestrator/services/run_service.py", "read_handoff_file", 14),
    ("DoD 7 evidence", "src/orchestrator/services/run_service.py", "delivery_evidence", 24),
    ("DoD 1/2 salvage", "src/orchestrator/services/run_service.py", "try_salvage_handoff", 55),
    (
        "DoD 3 archive",
        "src/orchestrator/services/run_service.py",
        "archive_before_round_start",
        18,
    ),
    ("DoD 1/4 gate", "src/orchestrator/services/run_service.py", "_gate_salvaged", 20),
    ("DoD 4 routing", "src/orchestrator/services/supervisor_service.py", "after_run", 18),
    (
        "DoD 3/7 render",
        "src/orchestrator/services/artifact_service.py",
        "render_delivery_evidence_md",
        24,
    ),
]

# (label, path, needle, context lines before/after)
ANCHORS = [
    ("DoD 1 cancel call", "src/orchestrator/services/run_service.py", "salvaged = try_salvage_handoff", 4),
    ("DoD 2 event emit", "src/orchestrator/services/run_service.py", '"handoff_salvaged",', 3),
    ("DoD 2 payload", "src/orchestrator/services/run_service.py", "def _salvage_log_payload", 3),
    ("DoD 3 clean-exit stale", "src/orchestrator/services/run_service.py", "stale_unchanged =", 12),
    ("DoD 3 round-start snapshot", "src/orchestrator/services/run_service.py", "stale_handoff = handoff_snapshot", 3),
    ("DoD 4 status const", "src/orchestrator/services/artifact_service.py", "HANDOFF_STATUS_SALVAGED =", 2),
    ("DoD 4 docs heading", "docs/build-loop.md", "Lifecycle: SALVAGED is not", 2),
    ("DoD 2 docs event", "docs/build-loop.md", "handoff_salvaged` is a new", 2),
    ("DoD 2 docs event list", "docs/build-loop.md", "`handoff_salvaged`,", 1),
    ("DoD 7 review wiring", "src/orchestrator/services/review_service.py", "delivery_evidence_section", 4),
    ("DoD 7 prompt wiring", "src/orchestrator/services/prompt_builder.py", "delivery_evidence", 3),
    ("DoD 6 no claims gate", "src/orchestrator/services/supervisor_service.py", "is deliberately NOT a verification check", 2),
    ("Scope no recovery gate", "src/orchestrator/services/supervisor_service.py", "T-8 owns the", 2),
]

TESTS = [
    "test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate",
    "test_cancel_with_a_valid_handoff_is_salvaged",
    "test_non_zero_exit_with_a_valid_handoff_is_salvaged",
    "test_timeout_with_no_handoff_is_exactly_todays_behaviour",
    "test_timeout_with_a_truncated_handoff_falls_back_and_logs_the_parse_failure",
    "test_a_pre_existing_submission_is_archived_before_the_round_start_unlink",
    "test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy",
    "test_a_stale_leftover_is_not_salvaged_as_the_new_rounds_submission",
    "test_a_clean_exit_does_not_ingest_a_stale_leftover_and_synthesizes",
    "test_a_salvaged_promise_is_flagged_in_the_evidence_not_turned_into_a_new_gate",
    "test_the_architect_prompt_for_a_salvaged_run_carries_the_delivery_evidence",
]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.rstrip()


def grep(path: str, needle: str, context: int) -> str:
    out = subprocess.run(
        ["grep", "-n", "-C", str(context), needle, path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.rstrip()
    return out or "(no match)"


def function(path: str, symbol: str, max_lines: int) -> str:
    lines = (ROOT / path).read_text().splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith((f"def {symbol}(", f"async def {symbol}("))),
        None,
    )
    if start is None:
        return f"--- {path}: SYMBOL NOT FOUND: {symbol}"
    end = min(start + max_lines, len(lines))
    return (
        f"--- {path}:{start + 1} def {symbol} (first {end - start} lines)\n"
        + "\n".join(lines[start:end])
    )


def main() -> None:
    head = git("rev-parse", "--short", "HEAD")
    merge_base = git("merge-base", "HEAD", "main")
    print(f"# HEAD={head} merge-base={merge_base} branch={git('rev-parse', '--abbrev-ref', 'HEAD')}")
    print("# command: python3 verify-evidence/T-10/collect_code_map.py")
    print()
    print("## Commits on the branch since the merge-base")
    print(git("log", "--oneline", "--no-decorate", f"{merge_base}..HEAD"))
    print()
    print("## Harness recovery commits: what they touched (content never self-checked)")
    for sha in git("log", "--format=%h", "--grep=orchestrator-recovery", f"{merge_base}..HEAD").splitlines():
        if sha:
            print(git("show", "--stat", "--oneline", "--format=%h %s", sha))
            print()
    print("## Diffstat since the merge-base")
    print(git("diff", "--stat", f"{merge_base}..HEAD"))
    print()
    print("## Cited functions (bodies truncated per function, file:line from THIS checkout)")
    for label, path, symbol, max_lines in FUNCTIONS:
        print(f"--- {label}: {path} {symbol}")
        print(function(path, symbol, max_lines))
        print()
    print("## Cited anchors")
    for label, path, needle, context in ANCHORS:
        print(f"--- {label}: {path} matching {needle!r}")
        print(grep(path, needle, context))
        print()
    print("## T-10 test functions (file:line from THIS checkout)")
    for name in TESTS:
        out = subprocess.run(
            ["grep", "-rn", f"def {name}", "tests"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.rstrip()
        print(out or f"MISSING: {name}")
    # Repeated at the tail on purpose: a middle-truncated diff keeps its last ~16KB, and the
    # commit list is the evidence the reviewer asked for by name.
    print()
    print("## Commits since the merge-base (repeated at the tail for truncated diffs)")
    print(git("log", "--oneline", "--no-decorate", f"{merge_base}..HEAD"))


if __name__ == "__main__":
    main()