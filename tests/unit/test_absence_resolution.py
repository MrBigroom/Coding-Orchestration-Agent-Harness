"""T-22: an absence claim is resolved to a path before it is judged.

Each test pins ONE outcome, because the bug being fixed is a machine check that was wrong in the
confident direction: on reviews.28 (run 38, T-19) 15 verdicts printed `confirmed` — the strongest
label available — against bare filenames whose tracked paths (`verify-evidence/T-19/<name>`) existed
at the reviewed commit. So the assertions here are about three separate things:

  - resolution: which path the claim was judged at, and which sources were searched (DoD 1/2);
  - state: which of the three reviewer-facing buckets the verdict belongs to (DoD 3);
  - honesty of the tally: a confident batch must be visibly resting on resolved paths or not
    (DoD 4).

Offline and mock-only: local git in a temp repo, no network, no provider call.
"""

from __future__ import annotations

from orchestrator.core.schemas import ReviewFinding
from orchestrator.services import review_audit
from tests.conftest import make_repo, requires_git
from tests.fixtures import absence_run38 as run38

pytestmark = requires_git

EVIDENCE_DIR = run38.TRACKED_EVIDENCE_DIR
INCIDENT_NAMES = [
    "capture.sh",
    "t19_tests.txt",
    "coverage_mutation.txt",
    "make_test_parity.txt",
    "full_suite.txt",
    "lint.txt",
]


def _supplied_for(repo, *, omitted=(), partial=(), full=()):
    """A supplied-evidence view of `repo`: `omitted` files never reached the reviewer."""
    patch = run38.patch_with_full_sections([*full, *partial])
    truncations = [
        {
            "section": "dynamic_state",
            "item": f"diff:{path}",
            "bytes_available": 3779,
            "bytes_included": 0,
            "budget": "max_diff_tokens=12000 tokens",
            "reason": "omitted because the diff budget was exhausted by higher-priority files",
        }
        for path in omitted
    ] + [
        {
            "section": "dynamic_state",
            "item": f"diff:{path}",
            "bytes_available": 2790,
            "bytes_included": 558,
            "budget": "max_diff_tokens=12000 tokens",
            "reason": "per-file diff budget: 1 of 1 hunks, body cut inside the shown hunk",
        }
        for path in partial
    ]
    return review_audit.supplied_evidence(patch, truncations, {*omitted, *partial, *full})


# --- DoD 1/2: resolve first, and never confirm what the lookup could not place ---------


def test_a_bare_name_that_exists_at_a_nested_path_is_not_confirmed_absent(tmp_path):
    """The run-38 shape: `capture.sh` is tracked at verify-evidence/T-19/capture.sh."""
    repo = make_repo(
        tmp_path / "repo",
        {
            "README.md": "# project\n",
            f"{EVIDENCE_DIR}/capture.sh": "#!/bin/sh\necho captured\n",
        },
    )
    finding = ReviewFinding(
        file_or_area="verify-evidence",
        issue="capture.sh is missing from the evidence directory",
        violates="DoD 4",
    )
    checks = review_audit.check_findings([finding], repo)

    assert checks[0].status == "refuted"
    assert checks[0].resolved_path == f"{EVIDENCE_DIR}/capture.sh"
    assert checks[0].basis == review_audit.BASIS_RESOLUTION
    assert checks[0].detail == (
        f"file is tracked at the reviewed commit at {EVIDENCE_DIR}/capture.sh"
    )


def test_a_bare_name_resolves_only_when_exactly_one_path_carries_it(tmp_path):
    """DoD 1 wording: 'when exactly one such file exists' — the resolver does not guess."""
    repo = make_repo(tmp_path / "repo", {f"{EVIDENCE_DIR}/full_suite.txt": "5 passed\n"})
    universe = review_audit.build_universe(repo, None)
    res = review_audit.resolve_claim_path("full_suite.txt", universe)

    assert res.path == f"{EVIDENCE_DIR}/full_suite.txt"
    assert res.candidates == ()
    assert "exactly one path" in res.reason


def test_a_genuinely_missing_nested_path_is_confirmed_with_the_sources_searched(tmp_path):
    """DoD 5(b): confirmation is still available — for a path the claim itself locates."""
    repo = make_repo(tmp_path / "repo", {"README.md": "# project\n"})
    supplied = _supplied_for(
        repo,
        omitted=[f"{EVIDENCE_DIR}/audit.txt"],
        partial=[f"{EVIDENCE_DIR}/partial.txt"],
        full=["README.md"],
    )
    finding = ReviewFinding(
        file_or_area=f"{EVIDENCE_DIR}/never_committed.txt",
        issue=f"{EVIDENCE_DIR}/never_committed.txt is missing",
        violates="DoD 5",
    )
    checks = review_audit.check_findings([finding], repo, supplied)

    assert checks[0].status == "confirmed"
    assert checks[0].state == review_audit.STATE_ABSENT
    assert checks[0].basis == review_audit.BASIS_EXPLICIT
    # every source that could name a path was searched, and says so in the row
    assert ", ".join(review_audit.ALL_SOURCES) in checks[0].detail


def test_a_bare_name_that_matches_nothing_is_unchecked_never_confirmed(tmp_path):
    """DoD 2: a lookup that found nothing is a failed lookup, not an absent file."""
    repo = make_repo(tmp_path / "repo", {"README.md": "# project\n"})
    finding = ReviewFinding(
        file_or_area="verify-evidence", issue="handoff.md is missing", violates="DoD 3"
    )
    checks = review_audit.check_findings([finding], repo)

    assert checks[0].status == "unchecked"
    assert checks[0].state == review_audit.STATE_UNRESOLVED
    assert "no path could be resolved from the claim" in checks[0].detail


def test_an_ambiguous_bare_name_lists_every_candidate_and_is_unchecked(tmp_path):
    """DoD 5(c): two nested files share the name, so the checker refuses to pick one."""
    repo = make_repo(
        tmp_path / "repo",
        {
            f"{EVIDENCE_DIR}/capture.sh": "# one\n",
            "scripts/capture.sh": "# two\n",
        },
    )
    finding = ReviewFinding(
        file_or_area="verify-evidence", issue="capture.sh is missing", violates="DoD 4"
    )
    checks = review_audit.check_findings([finding], repo)

    assert checks[0].status == "unchecked"
    assert checks[0].state == review_audit.STATE_UNRESOLVED
    assert set(checks[0].candidates) == {f"{EVIDENCE_DIR}/capture.sh", "scripts/capture.sh"}
    row = review_audit.render_absence_checks(checks)
    assert f"{EVIDENCE_DIR}/capture.sh" in row and "scripts/capture.sh" in row


def test_normalization_of_a_claimed_path_drops_relative_prefixes():
    assert review_audit.normalize_claim_path("./src/app.py") == "src/app.py"
    assert review_audit.normalize_claim_path("/src/app.py") == "src/app.py"
    assert review_audit.claim_is_explicit("verify-evidence/T-19/capture.sh") is True
    assert review_audit.claim_is_explicit("capture.sh") is False


# --- DoD 3: the three states a reviewer acts on ---------------------------------------


def test_a_file_omitted_by_the_budget_is_reported_as_the_budget_state(tmp_path):
    """DoD 5(d): present in the worktree, withheld by the budget — never labelled absent."""
    repo = make_repo(tmp_path / "repo", {f"{EVIDENCE_DIR}/lint.txt": "All checks passed!\n"})
    supplied = _supplied_for(repo, omitted=[f"{EVIDENCE_DIR}/lint.txt"])
    checks = review_audit.check_missing_artifacts(["lint.txt"], repo, supplied)

    # DoD 2: the path was located AND the repository contains it, so the strongest label is not
    # available. The state carries the budget meaning instead of a false confirmed absence.
    assert checks[0].status == "unchecked"
    assert checks[0].state == review_audit.STATE_OMITTED
    assert "not a Worker evidence gap" in checks[0].detail
    assert review_audit.STATE_ABSENT not in checks[0].detail


def test_a_changed_file_with_no_patch_section_is_the_budget_state_too(tmp_path):
    """The same state for a file the diff budget dropped without any manifest record."""
    repo = make_repo(tmp_path / "repo", {f"{EVIDENCE_DIR}/t19_tests.txt": "20 passed\n"})
    supplied = review_audit.supplied_evidence("", [], {f"{EVIDENCE_DIR}/t19_tests.txt"})
    checks = review_audit.check_missing_artifacts(["t19_tests.txt"], repo, supplied)

    assert checks[0].status == "unchecked"
    assert checks[0].state == review_audit.STATE_OMITTED
    assert "no section for it" in checks[0].detail


def test_a_path_that_is_not_in_the_repository_is_the_real_evidence_gap(tmp_path):
    """The second state: nothing to supply, because nothing exists."""
    repo = make_repo(tmp_path / "repo", {"README.md": "# project\n"})
    supplied = _supplied_for(repo, full=["README.md"])
    checks = review_audit.check_missing_artifacts(
        ["verify-evidence/T-22/no_such_proof.txt"], repo, supplied
    )

    assert checks[0].status == "confirmed"
    assert checks[0].state == review_audit.STATE_ABSENT
    assert "listed by none of the sources searched" in checks[0].detail


def test_the_three_states_have_distinct_wording_in_the_rendered_table(tmp_path):
    """DoD 3: an operator reading the archived table can tell the states apart by their words."""
    repo = make_repo(
        tmp_path / "repo",
        {
            "README.md": "# project\n",
            f"{EVIDENCE_DIR}/lint.txt": "lint passed\n",
        },
    )
    supplied = _supplied_for(repo, omitted=[f"{EVIDENCE_DIR}/lint.txt"], full=["README.md"])
    checks = review_audit.check_missing_artifacts(
        [
            "lint.txt",
            "verify-evidence/T-22/no_such_proof.txt",
            "totally_unrelated.txt",
        ],
        repo,
        supplied,
    )
    rendered = review_audit.render_absence_checks(checks)

    assert [c.state for c in checks] == [
        review_audit.STATE_OMITTED,
        review_audit.STATE_ABSENT,
        review_audit.STATE_UNRESOLVED,
    ]
    # each label appears as its own cell in the table, so the three states are not the same row
    assert (
        rendered.count(f"| {review_audit.STATE_OMITTED} |") == 1
        and rendered.count(f"| {review_audit.STATE_ABSENT} |") == 1
        and rendered.count(f"| {review_audit.STATE_UNRESOLVED} |") == 1
    )
    assert "not a Worker evidence gap" in rendered
    assert "listed by none of the sources searched" in rendered
    assert "no path could be resolved from the claim" in rendered


# --- DoD 4: the tally says what a confident verdict rests on --------------------------


def test_the_tally_splits_confirmed_by_resolution_from_confirmed_by_failed_lookup(tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {
            "README.md": "# project\n",
            "src/pkg/mod.py": "x = 1\n",
        },
    )
    findings = [
        # confirmed by an explicit-named path: nothing lists it anywhere
        ReviewFinding(file_or_area="x", issue="docs/absent.md is missing", violates=""),
        # refuted by resolution: the file is tracked
        ReviewFinding(file_or_area="x", issue="src/pkg/mod.py is missing", violates=""),
        # unchecked: a bare name the lookup cannot place
        ReviewFinding(file_or_area="x", issue="mystery.txt is missing", violates=""),
    ]
    checks = review_audit.check_findings(findings, repo)
    tally = review_audit.tally_verdicts(checks)

    assert tally.startswith("1 confirmed, 1 refuted, 1 unchecked ")
    assert "by explicit-named-path: 1" in tally
    assert "from a failed lookup: 0" in tally
    assert "1 unresolvable-claim" in tally


def test_no_confirmed_verdict_is_reachable_from_a_failed_lookup(tmp_path):
    """The invariant DoD 2 states, asserted directly rather than by example."""
    repo = make_repo(tmp_path / "repo", {"README.md": "# project\n"})
    findings = [
        ReviewFinding(file_or_area="a", issue="whatever.txt is missing", violates=""),
        ReviewFinding(file_or_area="b", issue="nested/deeper/thing.md is absent", violates=""),
        ReviewFinding(file_or_area="c", issue="there is no such thing here", violates=""),
    ]
    checks = review_audit.check_findings(findings, repo)

    assert [
        c.claim for c in checks if c.status == "confirmed" and c.basis == "failed-lookup"
    ] == []


def test_render_universe_names_every_source_and_the_reviewed_commit(tmp_path):
    repo = make_repo(tmp_path / "repo", {"README.md": "# project\n"})
    universe = review_audit.build_universe(repo, None)
    text = review_audit.render_universe(universe)

    assert "git ls-tree HEAD (1)" in text
    assert f"at commit {universe.head_sha}" in text


# --- DoD 5(e): the run-38 incident, reproduced as data --------------------------------


def _run38_checks(tmp_path):
    repo = make_repo(tmp_path / "repo", {p: "content\n" for p in run38.CHANGED_PATHS})
    omitted = [
        r["item"][5:] for r in run38.TRUNCATIONS if r["bytes_included"] == 0
    ]
    partial = [r["item"][5:] for r in run38.TRUNCATIONS if r["bytes_included"] > 0]
    full = [p for p in run38.CHANGED_PATHS if p not in omitted + partial]
    patch = run38.patch_with_full_sections(full)
    supplied = review_audit.supplied_evidence(patch, run38.TRUNCATIONS, set(run38.CHANGED_PATHS))
    universe = review_audit.build_universe(repo, supplied)
    checks = review_audit.check_findings(
        run38.FINDINGS, repo, supplied, universe
    ) + review_audit.check_missing_artifacts(
        run38.stripped_missing_artifacts(), repo, supplied, universe
    )
    return checks, supplied, universe


def test_run38_bare_evidence_names_resolve_to_their_tracked_paths(tmp_path):
    checks, _supplied, _universe = _run38_checks(tmp_path)
    by_name = {c.claim: c for c in checks if c.claim in INCIDENT_NAMES}

    assert set(by_name) == set(INCIDENT_NAMES)
    assert {name: by_name[name].resolved_path for name in INCIDENT_NAMES} == {
        name: f"{EVIDENCE_DIR}/{name}" for name in INCIDENT_NAMES
    }
    assert {name: by_name[name].state for name in INCIDENT_NAMES} == {
        name: review_audit.STATE_OMITTED for name in INCIDENT_NAMES
    }


def test_run38_fixture_yields_the_corrected_mix_and_no_false_absence(tmp_path):
    checks, _supplied, _universe = _run38_checks(tmp_path)
    tally = review_audit.tally_verdicts(checks)

    # reviews.28 reported "15 confirmed, 1 refuted, 2 unchecked". Every one of those confirmed
    # verdicts was against a file the repository contained, so the corrected mix confirms none:
    # the located files are the budget state, and README.md is ambiguous between the root file and
    # verify-evidence/T-19/README.md, so it is unresolved with both candidates listed.
    assert tally.startswith("0 confirmed, 1 refuted, 17 unchecked ")
    assert "from a failed lookup: 0" in tally
    assert "by path-resolution: 0" in tally
    assert "11 omitted-from-review-input" in tally
    assert "6 unresolvable-claim" in tally
    assert [
        c.claim for c in checks if c.status == "confirmed" and c.basis == "failed-lookup"
    ] == []
    # The exact run-38 wording is gone from every row of the archived table.
    rendered = review_audit.render_absence_checks(checks)
    assert (
        "path is absent from the task worktree, the supplied patch and the truncation manifest"
        not in rendered
    )


def test_run38_readme_ambiguity_is_unchecked_with_both_candidates(tmp_path):
    """Round-2 finding 1: a root README must not silently hide the nested evidence README."""
    checks, _supplied, _universe = _run38_checks(tmp_path)
    rows = [c for c in checks if c.claim == "README.md"]

    assert rows
    assert all(c.status == "unchecked" for c in rows)
    assert all(c.state == review_audit.STATE_UNRESOLVED for c in rows)
    assert all(
        set(c.candidates) == {"README.md", f"{EVIDENCE_DIR}/README.md"} for c in rows
    )
    rendered = review_audit.render_absence_checks(rows)
    assert rendered.count("README.md") >= 2


def test_run38_handoff_md_claim_was_a_false_absence_and_is_now_unresolved(tmp_path):
    """`absence check [confirmed] handoff.md` in run 38: the repo has no handoff.md at all."""
    checks, _supplied, _universe = _run38_checks(tmp_path)
    row = next(c for c in checks if c.claim == "handoff.md")

    assert row.status == "unchecked"
    assert row.state == review_audit.STATE_UNRESOLVED
    assert row.resolved_path == ""


# --- round-2 findings 1 and 3: ambiguity and the reviewed commit, not the worktree -------


def test_a_bare_name_with_a_root_and_nested_match_is_ambiguous_not_root(tmp_path):
    """A root file is just one candidate; it must not win by being an exact match."""
    repo = make_repo(
        tmp_path / "repo",
        {"README.md": "# root\n", f"{EVIDENCE_DIR}/README.md": "# evidence\n"},
    )
    universe = review_audit.build_universe(repo, None)
    res = review_audit.resolve_claim_path("README.md", universe)

    assert res.path == ""
    assert set(res.candidates) == {"README.md", f"{EVIDENCE_DIR}/README.md"}
    assert "none was picked" in res.reason


def test_an_untracked_worktree_file_is_absent_at_the_reviewed_commit(tmp_path):
    """Round-2 finding 3: only `git ls-tree HEAD` says what the reviewed commit contains."""
    repo = make_repo(tmp_path / "repo", {"README.md": "# project\n"})
    untracked = repo / EVIDENCE_DIR / "uncommitted.txt"
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("not committed\n")
    path = f"{EVIDENCE_DIR}/uncommitted.txt"

    finding = ReviewFinding(file_or_area=path, issue=f"{path} is missing", violates="DoD 5")
    checks = review_audit.check_findings([finding], repo)

    assert checks[0].status == "confirmed"
    assert checks[0].state == review_audit.STATE_ABSENT
    assert "not tracked at the reviewed commit" in checks[0].detail


def test_an_untracked_file_is_not_the_budget_state(tmp_path):
    """The visibility half of the same rule: an uncommitted file cannot be "in the repository"."""
    repo = make_repo(tmp_path / "repo", {"README.md": "# project\n"})
    untracked = repo / EVIDENCE_DIR / "uncommitted.txt"
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("not committed\n")
    supplied = review_audit.supplied_evidence("", [], set())

    checks = review_audit.check_missing_artifacts(
        [f"{EVIDENCE_DIR}/uncommitted.txt"], repo, supplied
    )

    assert checks[0].status == "confirmed"
    assert checks[0].state == review_audit.STATE_ABSENT
    assert "not in the repository at the reviewed commit" in checks[0].detail


def test_every_confirmed_verdict_names_a_path_the_repository_does_not_contain(tmp_path):
    """DoD 2 as an invariant over the incident fixture, not just one example."""
    checks, _supplied, _universe = _run38_checks(tmp_path)

    confirmed = [c for c in checks if c.status == "confirmed"]
    assert confirmed == []
    assert all(c.state == review_audit.STATE_ABSENT for c in confirmed)
