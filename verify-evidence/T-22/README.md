# T-22 evidence index

Code HEAD these files were captured at: `f45cf1fbc84da6d90fa4d3b3649c7900c90d26e6`
(`# HEAD=` header inside every `.txt`). The evidence files were committed after the suite ran, so
the stamp names the source that was tested, not the evidence commit.

Line citations below are explicit about which file they belong to. A number like
`resolver_index.txt:35` is a line in that evidence file; the `283` printed inside the line is a
**source line** in `src/orchestrator/services/review_audit.py`, not a line of the evidence file.

## Files

| file | what it proves |
|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` at the stamped HEAD: 641 passed, 9 skipped |
| `lint.txt` | `./scripts/orch_verify.sh lint`: All checks passed |
| `t22_tests.txt` | the focused run of the three T-22 test files: 46 passed |
| `replay.txt` | the archived reviews.28 input (run 38) through the new resolver: before/after and the tally |
| `mutation.txt` | 12 guarded tests run with the resolver broken in-process (m1-m6), each case red where expected, `all mutations detected: True` |
| `resolver_index.txt` | label vocabulary and the source line where each resolver function starts |
| `wiring.txt` | `review_service.py` actually calls the resolver and archives the tally/resolution columns |
| `docs.txt` | the DoD 6 doc sections, with source line numbers |
| `capture.sh` / `extractor.py` | reproducible capture (offline, temp repos, runtime patches only) |

## DoD item -> source -> test -> evidence

| DoD | implementation | regression test | evidence / log line |
|---|---|---|---|
| 1. resolve a claimed path first; bare name -> unique match, several -> all candidates unchecked | `review_audit.py:228` `build_universe`, `:283` `resolve_claim_path`, four sources at `:89-97` | `tests/unit/test_absence_resolution.py:66` (nested bare name), `:90` (unique match only), `:137` (ambiguous lists both), `:381` (root + nested is ambiguous) | `resolver_index.txt` tail (`resolve_claim_path`, 60 source lines); `replay.txt` (`README.md -> -`, unresolved); `replay.txt` (`capture.sh -> verify-evidence/T-19/capture.sh`); `t22_tests.txt:4,8,19` |
| 2. `confirmed` only for a located path every source denies; failed lookup or unavailable source is `unchecked` | `review_audit.py:580` `_confirmation_blockers`, `:611` `_confirmed_or_unchecked`, `:621` `_visibility_verdict`, `:820` `_existence_verdict`, `:1137` `tally_verdicts` | `test_absence_resolution.py:124` (nothing matched -> unchecked), `:168` (present+omitted -> unchecked), `:428` (empty source still searched), `:444` (ls-tree unavailable -> unchecked), `:468` (diffstat-only deletion -> unchecked), `:490` (invariant over non-empty confirmed rows) | `replay.txt` `TALLY: 0 confirmed, ... from a failed lookup: 0`; `replay.txt` `no confirmed verdict from a failed lookup: True`; `mutation.txt` m4 2 RED, m5 4 RED, m6 7 RED |
| 3. three distinct states, distinct wording | `review_audit.py:112-117` state labels; `_visibility_verdict` (`:621`); `_existence_verdict` (`:820`) | `test_absence_resolution.py:206` (three state cells), `:395` (untracked file is absent-at-commit, not omitted), `:411` (visibility half) | `replay.txt` `STATE COUNTS: ... omitted=11, unresolvable=6`; `t22_tests.txt:22,23,24` |
| 4. self-auditing tally + resolution evidence archived in `review_payload.md` | `review_audit.py:1137` `tally_verdicts`, `:1174` `render_universe`, `claim_note`/`found_note`, table renderer; `review_service.py:320,340,461,488` | `test_absence_resolution.py:246` (tally split), `:286` (universe names every source+commit); `tests/integration/test_review_payload.py:176` (payload carries resolution) | `wiring.txt:5,8,9,11,12,17-19`; `replay.txt` TALLY/STATE COUNTS; `t22_tests.txt` includes the payload test |
| 5. offline/mock-only regressions (a)-(e) from run 38 | `tests/fixtures/absence_run38.py` (verbatim incident data) | (a) `:66`; (b) `:101`; (c) `:137`; (d) `:168`; (e) `:329` and `:353`; round-3 additions `:444`, `:468`, `:490` | `t22_tests.txt:4,6,8,10,18,19`; `replay.txt` `0 confirmed/1 refuted/17 unchecked`; `mutation.txt` all six mutations detected |
| 6. docs state the mechanism and the resolver rule | `docs/build-loop.md:531-612` (resolve/labels/tally/visibility + operator rule), `docs/decisions.md:369-397` | — (documentation) | `docs.txt` (source-numbered excerpts); `replay.txt` is the tally the docs quote |

## Notes the reviewer asked for

- **Round-3 finding 1 — confirmed requires a full search.** `_confirmation_blockers`
  (`review_audit.py:580`) returns a blocker unless `git ls-tree HEAD` produced a commit SHA, every
  source in `ALL_SOURCES` was consulted, and no source names the resolved path. Both
  `_visibility_verdict` and `_existence_verdict` route every would-be `confirmed` through
  `_confirmed_or_unchecked` (`:611`), so an explicit-path miss with a failed `git ls-tree`, or a
  path the diffstat still names after a deletion, is `unchecked` / `undecided` with the blocker
  named. Tests: `:444` and `:468`.
- **Round-3 finding 3 — the invariant is exercised.** `test_every_confirmed_verdict_names_a_path_
  the_repository_does_not_contain` (`:490`) asserts two confirmed rows exist before checking that
  each is `absent-at-reviewed-commit`, absent from `git ls-tree`, has no `found_in`, and records all
  four sources. It replaces the vacuous `all(...)` over an empty list.
- **Round-3 finding 4 — consulted is not "contributed a path".** `PathUniverse.mark_searched`
  (`review_audit.py:170`) records a source when it is consulted, and `build_universe` marks the
  three archived sources even when they are empty and marks `git ls-tree HEAD` only when it
  succeeded. Unavailable sources are recorded with a reason and named by
  `unavailable_list()`/`render_universe`. Test: `:428`.
- **`AbsenceCheck.as_dict()` extra keys are not a frozen artifact.** The only caller is
  `review_service.py:445`, which puts the dict into the `findings_json=` segment of a
  `review_absence_refuted` build-log event. `review.json` is `ReviewResult.model_dump()` and never
  serialises `AbsenceCheck`; `review_payload.md` renders the table text, not this dict. No frozen
  shape changed.
- **Recovery commits.** `bc374b7` and `ff1dc88` were harness recovery commits containing
  `verify-evidence/T-22/*` only. Re-checked against the code: they introduced no source edits and
  no contract edits. Two defects in that undeclared content were repaired in the code/evidence
  commit: (1) the stale evidence was captured at `54cf4ab` and quoted `14 confirmed` for run 38,
  and its handoff cited source line numbers as if they were evidence-file lines; (2) the
  `--mutation` extractor still called `_check_visibility` with the old `cwd` first argument after
  the resolver signature changed. Evidence is re-captured at `f45cf1f`, this README distinguishes
  source from evidence lines, and `extractor.py` matches the current signature.
- **No new dependencies.** Evidence is produced by stdlib `inspect`/`subprocess` plus the
  project's own test helpers; `git ls-tree` and `git diff` are the only external commands.