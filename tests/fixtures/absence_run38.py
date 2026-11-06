"""The run-38 (reviews.28, T-19) absence-check incident, reproduced as data for T-22.

The mechanical checker reported `15 confirmed, 1 refuted, 2 unchecked` and printed rows such as

    absence check [confirmed] capture.sh: path is absent from the task worktree, the supplied
    patch and the truncation manifest

while `git ls-tree HEAD` listed `verify-evidence/T-19/capture.sh` and six siblings. The claims
reached the checker as BARE FILENAMES because the reviewer wrote one brace-expanded item, and the
path extractor turned `{README.md,capture.sh,...}` into seven names with no directory. The checker
looked for the literal name at the worktree root, found nothing, and returned the strongest verdict
it had. Everything below is copied from the archived artifacts of that run; the values are not
invented, so a regression here fails for the real reason and not for a paraphrase of it.

Sources: `~/.local/share/coding-orchestrator/runs/38/review.json` (findings, missing_artifacts,
notes) and `runs/38/review_payload.md` (truncation manifest rows), plus `runs/38/diffstat.txt`
(the changed-file set). Redaction is not applied here: no secret-assignment text survives.
"""

from __future__ import annotations

# The seven evidence files the reviewer said were missing, all tracked at the reviewed commit.
TRACKED_EVIDENCE_DIR = "verify-evidence/T-19"
TRACKED_EVIDENCE = [
    "README.md",
    "capture.sh",
    "coverage_mutation.txt",
    "full_suite.txt",
    "lint.txt",
    "make_test_parity.txt",
    "t19_tests.txt",
]

# `git diff --numstat main...HEAD` from runs/38/diffstat.txt, paths only.
CHANGED_PATHS = [
    "Makefile",
    "README.md",
    "docs/build-loop.md",
    "justfile",
    "scripts/orch_env_parity.sh",
    "src/orchestrator/cli.py",
    "src/orchestrator/core/config.py",
    "src/orchestrator/services/project_service.py",
    "src/orchestrator/services/supervisor_service.py",
    "tests/conftest.py",
    "tests/integration/test_cli.py",
    "tests/integration/test_env_hermeticity.py",
    "tests/unit/test_env_guard.py",
    "verify-evidence/T-19/README.md",
    "verify-evidence/T-19/capture.sh",
    "verify-evidence/T-19/coverage_mutation.txt",
    "verify-evidence/T-19/full_suite.txt",
    "verify-evidence/T-19/lint.txt",
    "verify-evidence/T-19/make_test_parity.txt",
    "verify-evidence/T-19/t19_tests.txt",
]

# The reviewer's own findings, verbatim from review.json["findings"] except that the two places
# where the model quoted a credential-assignment fragment are described rather than pasted: the
# credential redactor keys on the assignment NAME (a name containing a credential word has its
# value replaced), so a literal fixture line would arrive at a reviewer prompt with its own text
# rewritten. That editing does not touch any path claim, which is what this fixture is for.
FINDINGS = [
    {
        "file_or_area": "docs/build-loop.md, README.md",
        "issue": (
            "Omitted from the review diff by the budget manifest. DoD 6 content (env-free by "
            "construction, web/node_modules absence plus tsc recipe, other tracked-vs-ambient "
            "differences, README Known-limitations line) is supported only by handoff claims and "
            "cannot be reviewed."
        ),
        "violates": "DoD 6; review check 5/6 (evidence must be inspectable)",
    },
    {
        "file_or_area": "scripts/orch_env_parity.sh",
        "issue": (
            "The make/just parity target's script is truncated inside its only hunk. It copies "
            "the operator's real local env file, but its file mode, cleanup on every exit path, "
            "worktree handling in the shared repo, and absence of T-20 sweeping are all "
            "unreviewable. The only test is a string grep."
        ),
        "violates": "DoD 4; Security (secret handling)",
    },
    {
        "file_or_area": "tests/integration/test_env_hermeticity.py spawn tests",
        "issue": (
            "The twice-run subset parity, the seeded T-9 pair and the collection-time import tests "
            "are opt-in, so the gate skipped them (among the 9 skipped). Their pass is evidenced "
            "only by the omitted t19_tests.txt and a handoff claim; handoff.md does not inline the "
            "output as it says it does."
        ),
        "violates": "DoD 2, DoD 5; review check 5",
    },
    {
        "file_or_area": "src/orchestrator/services/project_service.py run_env_parity_checks",
        "issue": (
            "An OSError from the 0600 copy escapes doctor(), and the finally block then unlinks a "
            "file this invocation did not create."
        ),
        "violates": "DoD 4 robustness",
    },
    {
        "file_or_area": "tests/conftest.py _hermetic_environment",
        "issue": (
            "Patching the settings env_file does not reach subclasses created during collection, "
            "which still read the local env file in an operator checkout."
        ),
        "violates": "DoD 2 (nit: same leak class, outcomes unaffected today)",
    },
    {
        "file_or_area": "src/orchestrator/core/config.py settings_env_names",
        "issue": (
            "Relies on a private pydantic-settings helper. It is pinned by behavioural tests, but "
            "is fragile across upgrades."
        ),
        "violates": "DoD 1 (nit)",
    },
    {
        "file_or_area": "handoff.json memory_suggestions[0]",
        "issue": (
            "The redactor mangled the suggestion's own content, so the entry would be stored "
            "garbled."
        ),
        "violates": "Handoff hygiene / memory quality",
    },
]


# review.json["missing_artifacts"], with the `reviewer-asserted: ` label the harness adds stripped
# back off: these are the strings the reviewing model actually emitted.
MISSING_ARTIFACTS = [
    "reviewer-asserted: docs/build-loop.md diff (omitted by budget manifest)",
    "reviewer-asserted: README.md diff (omitted by budget manifest)",
    "reviewer-asserted: scripts/orch_env_parity.sh full body (truncated by per-file budget)",
    (
        "reviewer-asserted: verify-evidence/T-19/{README.md,capture.sh,t19_tests.txt,"
        "coverage_mutation.txt,make_test_parity.txt,full_suite.txt,lint.txt} (omitted by budget "
        "manifest)"
    ),
    "reviewer-asserted: any gate-executed run of the three ORCH_ENV_SPAWN_TESTS tests",
]

# The truncation manifest rows, verbatim in shape from review_payload.md. `diff:<path>` items are
# what render_diff_budget records; bytes_included == 0 means the file never reached the reviewer.
BUDGET = "max_diff_tokens=12000 tokens; per-file cap 585/5850 tokens"
OMITTED_REASON = "omitted because the diff budget was exhausted by higher-priority files"
TRUNCATIONS = [
    {
        "section": "dynamic_state",
        "item": "diff:scripts/orch_env_parity.sh",
        "bytes_available": 2790,
        "bytes_included": 558,
        "budget": "max_diff_tokens=12000 tokens; per-file=585 tokens; cap=5850",
        "reason": "per-file diff budget: 1 of 1 hunks, body cut inside the shown hunk",
    },
    {
        "section": "dynamic_state",
        "item": "diff:docs/build-loop.md",
        "bytes_available": 4910,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:README.md",
        "bytes_available": 931,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:verify-evidence/T-19/README.md",
        "bytes_available": 3514,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:verify-evidence/T-19/capture.sh",
        "bytes_available": 3779,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:verify-evidence/T-19/coverage_mutation.txt",
        "bytes_available": 771,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:verify-evidence/T-19/full_suite.txt",
        "bytes_available": 591,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:verify-evidence/T-19/lint.txt",
        "bytes_available": 549,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:verify-evidence/T-19/make_test_parity.txt",
        "bytes_available": 702,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
    {
        "section": "dynamic_state",
        "item": "diff:verify-evidence/T-19/t19_tests.txt",
        "bytes_available": 2462,
        "bytes_included": 0,
        "budget": BUDGET,
        "reason": OMITTED_REASON,
    },
]


def stripped_missing_artifacts() -> list[str]:
    """The reviewer's own strings, without the label the harness re-adds when archiving."""
    return [m.removeprefix("reviewer-asserted: ") for m in MISSING_ARTIFACTS]


def patch_with_full_sections(paths: list[str]) -> str:
    """A supplied patch that contains every listed path in full (the reviewer got these hunks)."""
    parts = []
    for p in paths:
        parts.append(
            f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,0 +1,2 @@\n+x\n+y\n"
        )
    return "".join(parts)
