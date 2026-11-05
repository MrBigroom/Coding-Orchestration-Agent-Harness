# T-22 handoff — round 3 (changes applied)

Branch: `task-22-the-absence-checker-confirms-missing-evidence-it`
Status: CHANGES_APPLIED — ready for review. Code HEAD `f45cf1f`; evidence commit `a30ce6b`.
Merge-base `a003447`; branch diff 19 files, +2612/-217.

## Findings addressed

1. **Confirmed needs a full search (finding 1).** `_confirmation_blockers` (`review_audit.py:580`)
   refuses confirmation unless `git ls-tree HEAD` produced a SHA, every source in `ALL_SOURCES`
   was consulted, and no source names the path. `_visibility_verdict` and `_existence_verdict`
   route every would-be `confirmed` through `_confirmed_or_unchecked` (`:611`), which downgrades
   to `unchecked`/`undecided` and names the blocker. Tests: ls-tree unavailable
   (`tests/unit/test_absence_resolution.py:444`, visibility + existence) and diffstat-only
   deletion (`:468`).
2. **Evidence inlined (finding 2).** The numbered verdict functions and the `build-loop.md`
   label/operator passage are quoted below at `f45cf1f`; the review diff budget omitted those
   hunks, so they do not need to be read from `patch.diff`.
3. **Invariant is exercised (finding 3).** `test_every_confirmed_verdict_names_a_path_the_
   repository_does_not_contain` (`:490`) asserts two confirmed rows exist before checking each
   row's state, `git ls-tree` absence, empty `found_in`, and all-four-sources provenance.
4. **Consulted != contributed (finding 4).** `PathUniverse.mark_searched` (`:170`) records every
   consulted source; `build_universe` (`:228`) marks the three archived sources even when empty
   and marks `git ls-tree` only when it succeeded; `unavailable_list()` (`:202`) names the rest.
   Test: `:428`.

## Inlined evidence: verdict functions (verbatim, numbered, at `f45cf1f`)

### PathUniverse: a consulted source counts as searched (review_audit.py:170-176, 202-207)
   170	    def mark_searched(self, source: str) -> None:
   171	        if source not in self.searched:
   172	            self.searched.append(source)
   173	        self.unavailable.pop(source, None)
   174	
   175	    def mark_unavailable(self, source: str, reason: str) -> None:
   176	        if source not in self.searched:
   202	    def unavailable_list(self) -> str:
   203	        """The sources that could not be consulted, with why — named in an unchecked verdict."""
   204	        return ", ".join(
   205	            f"{source} ({self.unavailable.get(source, 'unavailable')})"
   206	            for source in ALL_SOURCES
   207	            if source not in self.searched
        )

### build_universe marks each source even when it contributes no entries (review_audit.py:228-247)
228	def build_universe(cwd: Path | None, supplied: SuppliedEvidence | None) -> PathUniverse:
229	    """Collect the paths the reviewer was supplied, the diffstat, the manifest and git.
230	
231	    Every source is searched even when an earlier one already matches: a verdict has to be able
232	    to say which sources were checked, and `confirmed` absence is only honest against the whole
233	    set (T-22 DoD 1/2).
234	    """
235	    universe = PathUniverse()
236	    if supplied is not None:
237	        # Consulted even when empty: an empty patch is evidence that nothing was supplied, and
238	        # "contributed no entries" must not read as "was not searched" (round-3 finding 4).
239	        for source in (SOURCE_PATCH, SOURCE_DIFFSTAT, SOURCE_MANIFEST):
240	            universe.mark_searched(source)
241	        for path in supplied.diff_supplied:
242	            universe.add(path, SOURCE_PATCH)
243	        for path in supplied.changed_paths:
244	            universe.add(path, SOURCE_DIFFSTAT)
245	        for record in supplied.truncations:
246	            item = str(record.get("item", ""))
247	            if item.startswith("diff:"):

### _confirmation_blockers + _confirmed_or_unchecked (review_audit.py:580-619)
580	def _confirmation_blockers(path: str, universe: PathUniverse) -> str:
581	    """Why `confirmed` is not available for this path — empty string when it is.
582	
583	    A confirmed absence needs more than "git ls-tree HEAD does not list it" (round-3 finding 1).
584	    It needs every one of the four sources to have been consulted, and every one of them to say
585	    nothing about the path: a path the diffstat names but the commit no longer contains (a
586	    deletion) is not absent from all four sources, so the checker must not call it absent. When a
587	    source was unavailable, the blocker names it and the verdict stays `unchecked` (fail closed).
588	    """
589	    if not universe.head_sha:
590	        return (
591	            "cannot confirm absence: `git ls-tree HEAD` was unavailable, so no reviewed commit "
592	            "was searched"
593	        )
594	    missing = [s for s in ALL_SOURCES if s not in universe.searched]
595	    if missing:
596	        return (
597	            "cannot confirm absence: not every source could be searched (unavailable: "
598	            + universe.unavailable_list()
599	            + ")"
600	        )
601	    listed = universe.names(path)
602	    if listed:
603	        return (
604	            "cannot confirm absence: the path is listed by "
605	            + ", ".join(listed)
606	            + " and the repository does not contain it at the reviewed commit"
607	        )
608	    return ""
609	
610	
611	def _confirmed_or_unchecked(
612	    path: str, universe: PathUniverse, state: str, detail: str
613	) -> tuple[str, str, str]:
614	    """Return `confirmed` only when the claim was resolved and every source denies the path."""
615	    blocker = _confirmation_blockers(path, universe)
616	    if blocker:
617	        return STATUS_UNCHECKED, STATE_UNDECIDED, f"{blocker}; {detail}"
618	    return STATUS_CONFIRMED, state, detail
619	

## Inlined evidence: docs/build-loop.md (verbatim, numbered, at `f45cf1f`)

### docs/build-loop.md:531-560 — resolve first, then the three states
531	- **Resolve before judging (T-22 DoD 1/2).** Every path claim is first resolved against a
532	  `PathUniverse` built from the four sources that can name a path: the supplied patch, the git
533	  diffstat, the truncation manifest, and `git ls-tree -r HEAD` at the reviewed commit. The
534	  worktree filesystem is deliberately **not** a source: an untracked or gitignored file is not in
535	  the commit the reviewer saw, so counting it let a file missing from the commit be labelled a
536	  review-input omission instead of an evidence gap (round-2 finding 3). A bare name is matched by
537	  basename against the whole universe *before* any exact root match is accepted, so `README.md`
538	  with both a root file and `verify-evidence/T-19/README.md` is ambiguous rather than silently
539	  resolved to the root one (round-2 finding 1). Exactly one match is judged at that path; several
540	  matches are `unchecked` with **every** candidate listed — the resolver never picks one; no match
541	  and the claim was only a name is `unchecked` with the reason `no path could be resolved from the
542	  claim`. `confirmed` is reachable only for a claim that was located **and whose path every one
543	  of the four sources denies**, with `git ls-tree HEAD` actually searched at the reviewed commit:
544	  if a source was unavailable, or another source lists the path (a deletion that survives only in
545	  the diffstat), the verdict is `unchecked` and names the blocker — a confident label is never
546	  manufactured from a partial search. A path the repository contains is never confirmed absent,
547	  even when the diff budget withheld it; a failed lookup cannot reach `confirmed` at all. That is
548	  the invariant the tally prints.
549	- **Three states, not one label (T-22 DoD 3).** `confirmed` alone conflated a review-input failure
550	  with a Worker evidence gap, so each verdict also carries a state in its detail, the table's
551	  `state` column and the review notes: `omitted-from-review-input` (present in the repository, but
552	  the archived manifest says the diff budget did not carry it — the budget's problem, owned by
553	  T-16, and not a reason to send work back to the Worker), `absent-at-reviewed-commit` (nothing to
554	  supply, because nothing is in the repository at that commit — a real evidence gap),
555	  `unresolvable-claim` (the checker cannot judge it), plus `undecided`, `present-in-repository` and
556	  `supplied-in-full` for the other refuted/unchecked reasons. The statuses stay inside the frozen
557	  `confirmed | refuted | unchecked` vocabulary: `omitted-from-review-input` is `unchecked` (the
558	  repository contains the file, so absence cannot be confirmed), `absent-at-reviewed-commit` is
559	  `confirmed`, and a failed lookup is `unchecked`. The states are extra wording inside the existing
560	  notes/detail fields, which is the only route a label has without a contract-change request.

### docs/build-loop.md:609-612 — the operator-facing rule
609	The operator-facing rule: **a machine verdict is only as trustworthy as its resolver.** When a
610	run's absence tally is not credible, read the `state` and `how it was resolved` columns before
611	believing any `confirmed`, and treat a reviewer note that says "the harness confirmed it" as
612	unverified until the row's resolution evidence backs it up.

## Tests — real counts from committed logs

- `./scripts/orch_verify.sh test` → **641 passed, 9 skipped**;
  `verify-evidence/T-22/full_suite.txt` (HEAD `f45cf1f`, 0 dirty entries outside evidence dir).
- Focused T-22 run (`-vv`, three test files) → **46 passed**;
  `verify-evidence/T-22/t22_tests.txt`.
- `./scripts/orch_verify.sh lint` → **All checks passed**; `verify-evidence/T-22/lint.txt`.
- Mutation check: 12 tests × 6 runtime mutations, `all mutations detected: True`;
  `verify-evidence/T-22/mutation.txt` (m4 2 RED, m5 4 RED, m6 7 RED).
- Run-38 replay: `0 confirmed, 1 refuted, 17 unchecked` (11 omitted, 6 unresolvable);
  `verify-evidence/T-22/replay.txt`.

## DoD self-check

| # | item | met | evidence |
|---|---|---|---|
| 1 | Resolve claimed path against four sources; unique basename resolves, several list every candidate | yes | `build_universe`/`resolve_claim_path`; tests `:66,:90,:137,:381`; replay `capture.sh -> verify-evidence/T-19/capture.sh`, `README.md` ambiguous. |
| 2 | `confirmed` only for a located path absent from all four sources; failed lookup unchecked | yes | `_confirmation_blockers`/`_confirmed_or_unchecked`; tests `:124,:444,:468`; m4/m5/m6. |
| 3 | Three states have separate labels and wording | yes | `STATE_OMITTED/ABSENT/UNRESOLVED/UNDECIDED`; tests `:206,:395,:411`; replay `STATE COUNTS`. |
| 4 | Self-auditing tally and archived per-verdict resolution evidence | yes | `tally_verdicts`/`render_universe`; tests `:246,:286` and `test_review_payload.py:176`; `wiring.txt`. |
| 5 | Offline mock-only regressions (a)-(e) from run 38 | yes | `tests/fixtures/absence_run38.py`; tests `:66,:101,:137,:168,:329,:353`; replay/mutation logs. |
| 6 | Docs state mechanism and resolver rule | yes | inlined `build-loop.md:531-560,609-612`; `docs/decisions.md:369-397`; `docs.txt`. |

## Files, dependencies, assumptions

Files: 8 source/test/doc paths plus 11 `verify-evidence/T-22/` evidence paths, all present on the
branch (`git diff --name-only a003447..HEAD`). No new dependencies. Recovery commits
`bc374b7`/`ff1dc88` touched only `verify-evidence/T-22/*`; no source or contract change was found.

- `SuppliedEvidence()` (empty sets) means the archived input was consulted and named no path;
  `None` means no archived input, so its three sources are unavailable and absence is never
  confirmed. A line-number claim inside a tracked file is still `confirmed` when the line count
  disproves it (a line claim, not an absent path).

Questions: none. Blockers: none. Status is CHANGES_APPLIED; the reviewer gate decides.
