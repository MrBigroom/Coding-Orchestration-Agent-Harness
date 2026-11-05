# T-22 evidence index

Code HEAD these files were captured at: `d7e35fd744d1db12509de913ac9cbd35db3ed26e`
(`# HEAD=` header inside every `.txt`). The evidence files were committed after the suite ran, so
the stamp names the source that was tested, not the evidence commit.

Line citations below are explicit about which file they belong to. A number like
`resolver_index.txt:30` is a line in the 39-line evidence file; the `245` printed inside that line
is a **source line** in `src/orchestrator/services/review_audit.py`, not a line of the evidence
file. (The previous round's handoff confused the two, which is why the reviewer could not follow
`resolver_index.txt` lines 166-290 in a 40-line file.)

## Files

| file | what it proves |
|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` at the stamped HEAD: 638 passed, 9 skipped |
| `lint.txt` | `./scripts/orch_verify.sh lint`: All checks passed |
| `t22_tests.txt` | the focused run of the three T-22 test files: 43 passed |
| `replay.txt` | the archived reviews.28 input (run 38) through the new resolver: before/after and the tally |
| `mutation.txt` | the runner broke the resolver in-process and each guarded test went red (not just green-by-default) |
| `resolver_index.txt` | label vocabulary and the source line where each resolver function starts |
| `wiring.txt` | `review_service.py` actually calls the resolver and archives the tally/resolution columns |
| `docs.txt` | the DoD 6 doc sections, with source line numbers |
| `capture.sh` / `extractor.py` | reproducible capture (offline, temp repos, runtime patches only) |

## DoD item -> source -> test -> evidence

| DoD | implementation | regression test | evidence / log line |
|---|---|---|---|
| 1. resolve a claimed path first; bare name -> unique match, several -> all candidates unchecked | `review_audit.py:245` `resolve_claim_path`, `:203` `build_universe`, four sources at `:87-96` | `tests/unit/test_absence_resolution.py:66` (nested bare name), `:90` (unique match only), `:137` (ambiguous lists both), `:381` (root + nested is ambiguous) | `resolver_index.txt:30` (`resolve_claim_path`, 59 source lines); `replay.txt:30` (`README.md -> -`, unresolved); `replay.txt:40` (`capture.sh -> verify-evidence/T-19/capture.sh`); `t22_tests.txt:4,8,19` |
| 2. `confirmed` only for a located path shown absent; a failed lookup is `unchecked` | `review_audit.py:541` `_visibility_verdict`, `:736` `_existence_verdict`, `:1050` `tally_verdicts` | `test_absence_resolution.py:124` (nothing matched -> unchecked), `:168` (present+omitted -> unchecked), `:271` (no confirmed from failed lookup) | `replay.txt:48` `TALLY: 0 confirmed, ... from a failed lookup: 0`; `replay.txt:51` `no confirmed verdict from a failed lookup: True`; `mutation.txt:25` (m2 3 RED), `:34` (m3 5 RED) |
| 3. three distinct states, distinct wording | `review_audit.py:110-115` state labels; `_visibility_verdict` (`:541`); `_existence_verdict` (`:736`) | `test_absence_resolution.py:206` (three state cells), `:395` (untracked file is absent-at-commit, not omitted), `:411` (visibility half) | `replay.txt:49` `STATE COUNTS: ... omitted=11, unresolvable=6`; `t22_tests.txt:22,23,24` |
| 4. self-auditing tally + resolution evidence archived in `review_payload.md` | `review_audit.py:1050` `tally_verdicts`, `:1013` table header, `:384` `claim_note`, `:1087` `render_universe`; `review_service.py:320,340,461,488` | `test_absence_resolution.py:246` (tally split), `:286` (universe names every source+commit); `tests/integration/test_review_payload.py:176` (payload carries resolution) | `wiring.txt:5,8,9,11,12,17-19`; `replay.txt:48-49`; `t22_tests.txt` focused run includes the payload test |
| 5. offline/mock-only regressions (a)-(e) from run 38 | `tests/fixtures/absence_run38.py` (verbatim incident data) | (a) `:66`; (b) `:101`; (c) `:137`; (d) `:168`; (e) `:329` and `:353` | `t22_tests.txt:4,6,8,10,18,19`; `replay.txt:48` (0 confirmed/1 refuted/17 unchecked); `mutation.txt:44` all mutations detected |
| 6. docs state the mechanism and the resolver rule | `docs/build-loop.md:531-610` (resolve/if labels/tally/visibility + operator rule), `docs/decisions.md:369-389` | — (documentation) | `docs.txt` (source-numbered excerpts); `replay.txt:48` is the tally the docs quote |

## Notes the reviewer asked for

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
  the resolver signature changed. Both are fixed here: evidence is re-captured at `d7e35fd`, this
  README distinguishes source from evidence lines, and `extractor.py` matches the current
  signature (mutation mode runs all four cases in `mutation.txt`).
- **No new dependencies.** Evidence is produced by stdlib `inspect`/`subprocess` plus the
  project's own test helpers; `git ls-tree` and `git diff` are the only external commands.