# T-22 evidence index

Code HEAD these files were captured at: `cf417519e866914213adc4b88518499ac7101e72`
(`# HEAD=` header inside every `.txt`). The evidence files were committed after the suite ran, so
the stamp names the source that was tested, not the evidence commit. Regenerate with
`./verify-evidence/T-22/capture.sh`; it writes every file below from the current HEAD.

Line citations below are explicit about which file they belong to. A number like
`resolver_index.txt:20` is a line in that evidence file; the `315` printed inside the line is a
**source line** in `src/orchestrator/services/review_audit.py`, not a line of the evidence file.

## Files

| file | what it proves |
|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` at the stamped HEAD: 644 passed, 9 skipped |
| `lint.txt` | `./scripts/orch_verify.sh lint`: All checks passed |
| `t22_tests.txt` | the focused run of the three T-22 test files: 49 passed |
| `replay.txt` | the archived reviews.28 input (run 38) through the new resolver: before/after and the tally |
| `mutation.txt` | 15 guarded tests run with the resolver broken in-process (m1-m8), each case red where expected, `all mutations detected: True` |
| `resolver_index.txt` | label vocabulary and the source line where each resolver function starts |
| `wiring.txt` | `review_service.py` actually calls the resolver and archives the tally/resolution columns |
| `docs.txt` | the DoD 6 doc sections, with source line numbers, read from the committed docs |
| `capture.sh` / `extractor.py` | reproducible capture (offline, temp repos, runtime patches only) |

## DoD item -> source -> test -> evidence

| DoD | implementation | regression test | evidence / log line |
|---|---|---|---|
| 1. resolve a claimed path first; bare name -> unique match, several -> all candidates unchecked; an explicit miss searches name and suffix | `review_audit.py:244` `build_universe`, `:296` `_name_candidates`, `:315` `resolve_claim_path`, four sources at `:89-97` | `test_absence_resolution.py:66` (nested bare name), `:90` (unique match only), `:124` (explicit partial path `T-19/capture.sh` -> unchecked with the nested candidate), `:159` (every explicit candidate listed), `:189` (ambiguous bare name lists both), `:433` (root + nested is ambiguous) | `resolver_index.txt` tail (`_name_candidates`, `resolve_claim_path`); `replay.txt` (`README.md -> -`; `capture.sh -> verify-evidence/T-19/capture.sh`); `t22_tests.txt:4-9,22` |
| 2. `confirmed` only for a located path every source denies; failed lookup or unavailable source is `unchecked`; tracked paths without a reviewed SHA do not count as searched | `review_audit.py:228` `_add_tracked_paths`, `:631` `_confirmation_blockers`, `:662` `_confirmed_or_unchecked`, `:672` `_visibility_verdict`, `:871` `_existence_verdict`, `:1188` `tally_verdicts` | `test_absence_resolution.py:176` (nothing matched -> unchecked), `:220` (present+omitted -> unchecked), `:480` (empty source still searched), `:496` (ls-tree unavailable -> unchecked), `:520` (tracked list without HEAD SHA -> unavailable), `:546` (diffstat-only deletion -> unchecked), `:568` (invariant over non-empty confirmed rows) | `replay.txt` `TALLY: 0 confirmed, ... from a failed lookup: 0`; `mutation.txt` m4 3 RED, m5 5 RED, m6 7 RED, m8 2 RED |
| 3. three distinct states, distinct wording | `review_audit.py:112-117` state labels; `_visibility_verdict` (`:672`); `_existence_verdict` (`:871`) | `test_absence_resolution.py:258` (three state cells), `:447` (untracked file is absent-at-commit, not omitted), `:463` (visibility half) | `replay.txt` `STATE COUNTS: ... omitted=11, unresolvable=6`; `t22_tests.txt` state tests |
| 4. self-auditing tally + resolution evidence archived in `review_payload.md` | `review_audit.py:1188` `tally_verdicts`, `:1225` `render_universe`, `claim_note`/`found_note`, table renderer; `review_service.py:320,340,461,488` | `test_absence_resolution.py:298` (tally split), `:338` (universe names every source+commit); `tests/integration/test_review_payload.py:176` (payload carries resolution) | `wiring.txt:5,8,9,11,12,17-19`; `replay.txt` TALLY/STATE COUNTS; `t22_tests.txt:51-52` (both payload tests pass) |
| 5. offline/mock-only regressions (a)-(e) from run 38 | `tests/fixtures/absence_run38.py` (verbatim incident data) | (a) `:66`; (b) `:101`; (c) `:189`; (d) `:220`; (e) `:368` and `:381`; round-3 additions `:480`, `:496`, `:546`, `:568`; round-4 additions `:124`, `:159`, `:520` | `t22_tests.txt:4-13,18-29`; `replay.txt` `0 confirmed/1 refuted/17 unchecked`; `mutation.txt` all eight mutations detected |
| 6. docs state the mechanism and the resolver rule | `docs/build-loop.md:531-621` (resolve/labels/tally/visibility + operator rule), `docs/decisions.md:369-403` | — (documentation) | `docs.txt` (source-numbered excerpts at this HEAD); `replay.txt` is the tally the docs quote |

## Round-4 findings (the reviewed round before this one)

- **Finding 1 — an explicit miss was conclusive.** `resolve_claim_path` (`review_audit.py:315`)
  now searches `_name_candidates` (`:296`) on an explicit miss. `T-19/capture.sh` therefore
  resolves to no single path but lists `verify-evidence/T-19/capture.sh` as a candidate, and the
  verdict is `unchecked` / `unresolvable-claim`; only a miss whose name matches nothing at all
  reaches `BASIS_EXPLICIT` and can be confirmed. Tests `:124` and `:159`; mutation `m7` (2 RED of
  15).
- **Finding 2 — the committed `handoff.md`.** The harness recovery commit `1a12d24` had added a
  repo-root `handoff.md` that no `files_changed` entry claimed. It is deleted on this branch (the
  removal is in commit `cf41751`), so it cannot merge into `main`.
- **Finding 3 — evidence omitted by the diff budget.** The label definitions, the tally format and
  the resolver-trust sentence are quoted in `handoff.json` `dod_self_check[].evidence` too, so the
  submission carries them even when the branch diff is truncated. The archived payload and the
  tally are proven by `t22_tests.txt` and `wiring.txt`, not only by prose.
- **Finding 4 — `ls-tree` without a SHA.** `_add_tracked_paths` (`review_audit.py:228`) only adds
  the tracked paths when `rev-parse HEAD` produced a SHA; otherwise `mark_unavailable` stands, so
  the source is not listed as searched and `_confirmation_blockers` refuses `confirmed`. Test
  `:520`; mutation `m8` (2 RED of 15).

## Recovery-commit content

`bc374b7` and `ff1dc88` (run R-40) added only `verify-evidence/T-22/*`; `1a12d24` (run R-43) added
the repo-root `handoff.md`, now deleted. No source or contract file was touched by a recovery
commit. Re-checked against the code at `cf41751`: the recovery evidence was captured at an older
HEAD, quoted `14 confirmed` for run 38, and the `--mutation` extractor called `_check_visibility`
with the pre-resolver signature. Both were repaired in the code/evidence commits; this README and
every `.txt` are re-captured at `cf41751`.

## No new dependencies

Evidence is produced by stdlib `inspect`/`subprocess`/`tempfile` plus the project's own test
helpers; `git ls-tree`, `git diff` and `git show` are the only external commands.