"""Final-source, wiring and before/after evidence for T-22, extracted from THIS checkout.

Run it via `verify-evidence/T-22/capture.sh`; each mode writes one committed `.txt`.

  --index      the label vocabulary plus a file:line index of every new function. Deliberately an
               index and not a source dump: the source is already in the branch diff, and pasting
               it back in only widens the truncation the reviewer has to work around.
  --wiring     the `review_service.py` lines that prove the resolver is actually used, and that
               review.json/review_payload.md carry the tally and the resolution columns
  --replay     the archived reviews.28 (run 38) input through the new resolver: before/after
  --mutation   the same checks re-run with the resolver broken in-process, so a green suite is not
               the only proof that the assertions bite

Offline: local git, temp repos, no network, no provider call. Nothing here edits the checkout: the
mutations are runtime patches inside this process.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from orchestrator.services import review_audit  # noqa: E402
from tests.conftest import make_repo  # noqa: E402
from tests.fixtures import absence_run38 as run38  # noqa: E402

EVIDENCE_DIR = Path(__file__).parent
REPO = EVIDENCE_DIR.parent.parent

# Ordered so the file reads as the decision path: normalize -> universe -> resolve -> judge -> tally.
RESOLVER_TARGETS = [
    ("class", "PathUniverse"),
    ("def", "build_universe"),
    ("def", "resolve_claim_path"),
    ("def", "_repo_present"),
    ("def", "_visibility_verdict"),
    ("def", "_existence_verdict"),
    ("def", "tally_verdicts"),
    ("def", "render_universe"),
]


def head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=REPO
    ).stdout.strip()


WIRING_NEEDLES = [
    "universe = review_audit.build_universe(cwd, supplied)",
    "result.findings, cwd, supplied, universe",
    "review_audit.check_missing_artifacts(model_missing, cwd, supplied, universe)",
    "+ review_audit.tally_verdicts(absence_checks)",
    "f\"({check.claim_note()})\"",
    "universe=universe,",
    "{review_audit.render_supplied_summary(supplied, universe)}",
    "Tally: {review_audit.tally_verdicts(absence_checks)}",
]


def print_index() -> None:
    """The vocabulary and a file:line index, without duplicating the diff."""
    print(f"# HEAD={head()}")
    print("# command: python verify-evidence/T-22/extractor.py --index")
    print("# every label a verdict can carry, and where each new function starts")
    src = (REPO / "src/orchestrator/services/review_audit.py").read_text().splitlines()
    print("\n----- review_audit.py label vocabulary -----")
    for i, line in enumerate(src, 1):
        stripped = line.strip()
        if stripped.startswith(("SOURCE_", "ALL_SOURCES", "STATUS_", "STATE_", "BASIS_")) and (
            "=" in stripped or stripped.startswith("ALL_SOURCES")
        ):
            print(f"{i:5d} | {stripped}")
        if stripped.startswith(('"supplied patch"', '"diffstat"', '"truncation manifest"')):
            print(f"{i:5d} | {stripped}")
    print("\n----- new functions, with their line counts (read them in the diff at these lines) -----")
    for kind, name in RESOLVER_TARGETS:
        obj = getattr(review_audit, name)
        lines, start = inspect.getsourcelines(obj)
        path = inspect.getsourcefile(obj).replace(str(REPO) + "/", "")
        print(f"{start:5d} | {path} {kind} {name} ({len(lines)} lines)")
    for name in ("_unresolved_claim", "_judged", "_check_visibility", "_check_existence_claim"):
        obj = getattr(review_audit, name)
        _lines, start = inspect.getsourcelines(obj)
        print(f"{start:5d} | src/orchestrator/services/review_audit.py def {name}")


def print_wiring() -> None:
    print(f"# HEAD={head()}")
    print("# command: python verify-evidence/T-22/extractor.py --wiring")
    print("# review_service.py lines that put the resolver and the evidence into the artifacts")
    src = (REPO / "src/orchestrator/services/review_service.py").read_text().splitlines()
    for needle in WIRING_NEEDLES:
        hit = next((i for i, line in enumerate(src, 1) if needle in line), None)
        print(f"{hit if hit else 'MISSING!':>5} | {needle}")
    print("\n# review_audit.py: the table columns carrying the resolution evidence")
    audit = (REPO / "src/orchestrator/services/review_audit.py").read_text().splitlines()
    for needle in [
        "| claim | source | status | checked against / evidence | state | how it was resolved | ",
        "def found_note(self) -> str:",
        "def claim_note(self) -> str:",
        "def tally_verdicts(checks: list[AbsenceCheck]) -> str:",
        "def render_universe(universe: PathUniverse) -> str:",
        "def resolve_claim_path(raw: str, universe: PathUniverse) -> PathResolution:",
    ]:
        hit = next((i for i, line in enumerate(audit, 1) if needle in line), None)
        print(f"{hit if hit else 'MISSING!':>5} | {needle}")
    print("\n# the frozen runs/<run_id>/ artifact names this evidence lands in (no new file)")
    # Search the joined source, not line-by-line: the call this proves spans three lines, and a
    # per-line scan reported it MISSING while the wiring was present.
    joined = "\n".join(src)
    for needle in [
        'artifact_service.write_text(\n        run.id,\n        "review_payload.md",',
        '"review_payload.md"',
    ]:
        offset = joined.find(needle)
        hit = joined[:offset].count("\n") + 1 if offset >= 0 else None
        print(f"{hit if hit else 'MISSING!':>5} | {needle}")


def replay() -> tuple[list[review_audit.AbsenceCheck], review_audit.PathUniverse]:
    repo = make_repo(Path(tempfile.mkdtemp()) / "repo", {p: "x\n" for p in run38.CHANGED_PATHS})
    omitted = [r["item"][5:] for r in run38.TRUNCATIONS if r["bytes_included"] == 0]
    partial = [r["item"][5:] for r in run38.TRUNCATIONS if r["bytes_included"] > 0]
    full = [p for p in run38.CHANGED_PATHS if p not in omitted + partial]
    supplied = review_audit.supplied_evidence(
        run38.patch_with_full_sections(full), run38.TRUNCATIONS, set(run38.CHANGED_PATHS)
    )
    universe = review_audit.build_universe(repo, supplied)
    checks = review_audit.check_findings(
        run38.FINDINGS, repo, supplied, universe
    ) + review_audit.check_missing_artifacts(
        run38.stripped_missing_artifacts(), repo, supplied, universe
    )
    return checks, universe


# Verbatim from runs/38/review.json's notes (reviews.28) — the incident being regressed.
RUN38_NOTES = [
    "absence check [confirmed] capture.sh: path is absent from the task worktree, the supplied "
    "patch and the truncation manifest (source: missing_artifact)",
    "absence check [confirmed] t19_tests.txt: path is absent from the task worktree, the supplied "
    "patch and the truncation manifest (source: missing_artifact)",
    "absence check [confirmed] coverage_mutation.txt: path is absent from the task worktree, the "
    "supplied patch and the truncation manifest (source: missing_artifact)",
    "absence check [confirmed] make_test_parity.txt: path is absent from the task worktree, the "
    "supplied patch and the truncation manifest (source: missing_artifact)",
    "absence check [confirmed] full_suite.txt: path is absent from the task worktree, the supplied "
    "patch and the truncation manifest (source: missing_artifact)",
    "absence check [confirmed] lint.txt: path is absent from the task worktree, the supplied patch "
    "and the truncation manifest (source: missing_artifact)",
    "absence check [confirmed] handoff.md: path is absent from the task worktree, the supplied "
    "patch and the truncation manifest (source: finding)",
    "mechanical absence checks: 15 confirmed, 1 refuted, 2 unchecked (of 7 finding(s) kept, 0 "
    "refuted finding(s) removed)",
]

OLD_WORDING = "path is absent from the task worktree, the supplied patch"


def print_before_after() -> None:
    print(f"# HEAD={head()}")
    print("# command: python verify-evidence/T-22/extractor.py --replay")
    print("\n## BEFORE - runs/38/review.json notes, verbatim (the false confirmations)")
    for line in RUN38_NOTES:
        print(f"  {line}")
    print("\n## the same paths, tracked at the reviewed commit")
    for name in run38.TRACKED_EVIDENCE:
        print(f"  {run38.TRACKED_EVIDENCE_DIR}/{name}   (claimed absent as `{name}`)")
    checks, universe = replay()
    print("\n## AFTER - the same findings, missing_artifacts and manifest rows through T-22")
    print(review_audit.render_universe(universe))
    print()
    for c in checks:
        print(f"  [{c.status}/{c.state}/{c.basis}] {c.source}: {c.claim} -> {c.resolved_path or '-'}")
    print("\n  TALLY: " + review_audit.tally_verdicts(checks))
    counts = Counter((c.status, c.state) for c in checks)
    print("  STATE COUNTS: " + ", ".join(f"{k[0]}/{k[1]}={v}" for k, v in sorted(counts.items())))
    print("  run 38's old wording absent from every row: "
          + str(all(OLD_WORDING not in c.detail for c in checks)))
    print("  no confirmed verdict from a failed lookup: "
          + str(
              not any(
                  c.status == "confirmed" and c.basis == review_audit.BASIS_FAILED_LOOKUP
                  for c in checks
              )
          ))


def print_mutation() -> None:
    """Break the resolver in-process and show which new tests go red (assertion shape check)."""
    sys.path.insert(0, str(REPO / "tests" / "unit"))
    import test_absence_resolution as target

    cases = [
        "test_a_bare_name_that_exists_at_a_nested_path_is_not_confirmed_absent",
        "test_a_bare_name_that_matches_nothing_is_unchecked_never_confirmed",
        "test_an_ambiguous_bare_name_lists_every_candidate_and_is_unchecked",
        "test_a_file_omitted_by_the_budget_is_reported_as_the_budget_state",
        "test_run38_bare_evidence_names_resolve_to_their_tracked_paths",
        "test_run38_fixture_yields_the_corrected_mix_and_no_false_absence",
        "test_run38_handoff_md_claim_was_a_false_absence_and_is_now_unresolved",
        "test_the_tally_splits_confirmed_by_resolution_from_confirmed_by_failed_lookup",
    ]

    def run(name: str) -> str:
        tmp = Path(tempfile.mkdtemp())
        try:
            getattr(target, name)(tmp)
            return "pass"
        except AssertionError:
            return "RED"

    orig_resolve = review_audit.resolve_claim_path
    orig_visibility = review_audit._check_visibility

    def bare_as_explicit(raw: str, universe: review_audit.PathUniverse):
        tried = review_audit.normalize_claim_path(raw)
        return review_audit.PathResolution(
            raw, tried, tried, review_audit.BASIS_EXPLICIT, "unresolved: claimed name"
        )

    def omitted_as_absent(cwd, res, universe, index, source, supplied):
        if res.resolved and supplied is not None and res.path in supplied.diff_cut:
            return review_audit.AbsenceCheck(
                finding_index=index,
                claim=res.claim,
                status="confirmed",
                detail=OLD_WORDING + " and the truncation manifest",
                source=source,
                state=review_audit.STATE_ABSENT,
                resolved_path=res.path,
                basis=res.basis,
                tried=res.tried,
            )
        return orig_visibility(cwd, res, universe, index, source, supplied)

    def failed_lookup_gets_a_path(raw, universe):
        res = orig_resolve(raw, universe)
        if res.resolved:
            return res
        return review_audit.PathResolution(
            res.claim, res.tried, res.tried, review_audit.BASIS_FAILED_LOOKUP, res.reason
        )

    print(f"# HEAD={head()}")
    print("# command: python verify-evidence/T-22/extractor.py --mutation")
    print("# runtime patches only: the checkout is never edited. Each mutation re-breaks the")
    print("# behaviour one DoD 5 test guards, and the row says whether that test goes red.\n")
    plan = [
        ("baseline (unmutated)", 0),
        ("m1: no basename resolution - run 38's behaviour", len(cases)),
        ("m2: budget-omitted relabelled as absent", 3),
        ("m3: a failed lookup still gets a path (fail-open confidence)", 5),
    ]
    verdicts = []
    for label, expected in plan:
        if label.startswith("m1"):
            review_audit.resolve_claim_path = bare_as_explicit
        elif label.startswith("m2"):
            review_audit.resolve_claim_path = orig_resolve
            review_audit._check_visibility = omitted_as_absent
        elif label.startswith("m3"):
            review_audit.resolve_claim_path = failed_lookup_gets_a_path
            review_audit._check_visibility = orig_visibility
        else:
            review_audit.resolve_claim_path = orig_resolve
            review_audit._check_visibility = orig_visibility
        results = {name: run(name) for name in cases}
        red = sum(1 for v in results.values() if v == "RED")
        ok = red >= expected
        verdicts.append(ok)
        print(f"{label}: {red} RED of {len(cases)} (expected at least {expected}) -> "
              + ("MUTATION_DETECTED" if ok else "MUTATION_MISSED"))
        for name, value in results.items():
            print(f"    {value:>3}  {name}")
    review_audit.resolve_claim_path = orig_resolve
    review_audit._check_visibility = orig_visibility
    print()
    print("all four cases behave as claimed: " + str(all(verdicts)))


if __name__ == "__main__":
    if "--replay" in sys.argv:
        print_before_after()
    elif "--mutation" in sys.argv:
        print_mutation()
    elif "--wiring" in sys.argv:
        print_wiring()
    else:
        print_index()
