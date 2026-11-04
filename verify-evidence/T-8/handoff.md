# T-8 handoff — recovery commits after a timeout committed half-finished worker edits

**Branch** `task-8-recovery-commits-after-a-timeout-commit-half-fin` · **Status** `CHANGES_APPLIED` · **Phase** T-8 · **Round 6** — applies `reviews.6` (CHANGES_REQUESTED, 8 findings). **Base** `main`: merge-base `14a221c`, measured at render time on HEAD `f3da9cb` = 2 behind / 22 ahead of `main@360228a`, `git merge-tree --write-tree main HEAD` → no conflicts. **Not merged, not pushed.** The branch name renders `ta[REDACTED]` in ARCHIVED copies until DoD 10's anchor ships — the running harness is `main@360228a`, which redacts `details` and artifact text with the unanchored pattern; the on-disk `.orchestrator/handoff.json` here is byte-exact. **If this block reaches you truncated, the order is deliberate: DoD self-check first, then verdicts, counts and findings. Nothing in it is restated later, so a cut costs only the tail sections, and every evidence string also names a `verify-evidence/T-8/README.md` row.**

## Verdict digest — 10 items, then the trailing fields

| DoD | met | in one line |
| --- | --- | --- |
| 1 | ✅ | trailer `Orchestrator-Recovery: <run>` + structured `findings_json` inside `details` |
| 2 | ✅ | subject prefix `chore(orchestrator-recovery):` kept byte-exact; trailer added alongside |
| 3 | ✅ | next run's prompt lists the commits and requires re-verify (lint+test) before handoff |
| 4 | ✅ | reviewer sections + GUI `RecoveryNote`; unauthorized `review_payload.md` removed (finding 2) |
| 5 | ✅ | offline timeout sim; the F821 wreckage is caught by tests AND by the lint command |
| 6 | ✅ | `run_completed` added, additive only; emission split documented; T-9 trigger set written |
| 7 | ✅ | per-round fields cleared at round start; a row cannot say in_progress + RUN_TIMEOUT |
| 8 | ✅ | contract-edit detector = GRANT test (not mention); 4 named per-case proofs; → NEEDS_HUMAN |
| 9 | ✅ | tracked vs untracked dirt split by decision; both predicates tested |
| 10 | ✅ | credential patterns anchored with a lookbehind; names survive, keys still redact |

**New dependencies:** none (0). **Blockers:** 0 — none. **Questions:** 6. **Assumptions:** 9. **Files changed:** 37. **Contracts touched:** 9 entries — `docs/frozen-contracts.md` **NOT edited**; the two open contract questions are CCR-T8-01 and CCR-T8-02, both **PROPOSED**. Tests: `./scripts/orch_verify.sh test` → 421 passed / 0 failed / 6 skipped (`verify-evidence/T-8/full_suite.txt`).

## DoD self-check (item → met → evidence)

### 1. [x] DoD 1 — recovery commits are machine-identifiable (trailer + structured event field), not subject-matched.

Content arrived UNDECLARED in recovery commit `ba77a43`; verified by reading + running, not re-authored. `git_service.py:47` `RECOVERY_TRAILER_KEY`, `:49` `_RECOVERY_TRAILER`, `:82` `RecoveryCommit.trailer`, `:291` `recovery_commit_info`, `:335`/`:347` event payload + parser, `:383` `commits_against_base` sets `CommitInfo.recovery` (`:118`) from the TRAILER ONLY; `utils/structured_log.py:22` rides ` | findings_json={…}` inside the existing `details` string, so the frozen line shape is untouched. Proofs: `tests/unit/test_recovery_markers.py:33,58,97` (:58 = a hand-authored commit copying the subject is NOT detected), `dod1_recovery_machinery.txt` §§(a)(b).

### 2. [x] DoD 2 — the `chore(orchestrator-recovery):` subject prefix is unchanged byte-exact; the trailer is added alongside.

`git_service.py:46` `RECOVERY_SUBJECT_PREFIX = "chore(orchestrator-recovery):"`; `recovery_message` writes the prefix subject first and the trailer last. `test_recovery_markers.py:44` asserts the full subject string; `dod1_recovery_machinery.txt` §(b) prints a real commit body. Event name `recovery_commit` unchanged (`docs/build-loop.md:86`). `ba77a43`/`5a0cd1a` keep their original messages — not amended.

### 3. [x] DoD 3 — the next run on a branch with recovery commits is told, with the list and a mandatory re-verify before handoff.json.

In `ba77a43`, verified by reading. `run_service.py:324-343` builds `recovery_state` from `project_service.recovery_commits_for` + `git_service.format_recovery_commit_list:457` and logs `recovery_notice_included`; `prompt_builder.py:126-141` renders it in the cache-DYNAMIC tier. The text requires re-verifying with the project's lint/test commands, repairing or reporting every defect the swept commits introduced, and a BLOCKED handoff + contract-change request if a swept edit touched an unauthorized contract file. Proof: `tests/integration/test_recovery_after_timeout.py:191` asserts heading, `NEVER self-checked`, the re-verify instruction, the swept sha in `runs/<id>/prompt.md`, and that round 3 is told too.

### 4. [x] DoD 4 — reviewer payload and GUI mark recovery distinctly; the prompt says plainly that recovery content was never self-checked.

In `ba77a43`, amended round 6 (finding 2). `review_service.py:95-112` `recovery_section` ('created by the ORCHESTRATOR … NEVER self-checked') + `:114-137` `harness_findings_section`, rendered by `prompt_builder.py:126-144` as 'Recovery commits on this branch (created by the harness, NEVER self-checked by the Worker)' / 'Harness-detected findings'; `supervisor_service.py:47,271-287` `RECOVERY_CHECK`; GUI `TaskDetail.tsx:29-56` + `:95` typed `types.ts:85-93,101-102`, served ONLY by `api/routes/tasks.py:38,62-69`. ROUND 6 (finding 2): the `runs/<id>/review_payload.md` writer is DELETED (`review_service.py:162-166` says why) — it added a file to the FROZEN artifact layout with no DoD grant; `docs/build-loop.md:297-314` documents the gap and its one owner. Proofs: integration `:311-363` (both sections, the swept path, NO `review_payload.md` in the run dir, `GET /api/tasks/{id}/recovery` **404s** — ruling 3 deleted it, so nothing claims it is live — and every GUI-rendered value on the detail response); unit `:367`. PARTIAL, stated: whole-diff marking, not per-hunk. `dod4_gui_typecheck.txt`: `tsc` exit 0. Full row: README DoD 4.

### 5. [x] DoD 5 — offline tests simulate the timeout with a dirty worktree and prove a broken intermediate state is surfaced by verification, not accepted.

In `ba77a43` + `05011a4` (the LINT half, which `ba77a43` lacked), verified by running. `tests/unit/test_recovery_markers.py` (16) + `tests/integration/test_recovery_after_timeout.py` (5), all offline on temp git repos with mock harness/provider. `::test_timeout_wreckage_is_committed_as_recovery_and_told_to_the_next_run:191-309` leaves `def handle(): return not_yet_defined(1)` — run 5's actual defect class — and asserts the gate fails on BOTH commands: `tests` fail (`test.log` names `not_yet_defined`) AND `lint` fails (`lint.log`: `app.py:2: F821 undefined name 'not_yet_defined'`); the Worker's `READY_FOR_REVIEW`+`met: true` story is contradicted and no reviewer is consulted. Measured at this HEAD: 421/0/6 full suite, targeted 16/5/21/14/5/40 (`targeted_tests.txt`).

### 6. [x] DoD 6 — a named terminal event for a normally completed run, additive only, documented alongside the existing vocabulary, with the T-9 trigger set expressed against it.

Emitter in `ba77a43`; the docs it referenced but never wrote came in `232b6d4`, reworded in `21f79c4` + round 6. `run_service.py:70-90` — `_set_run` is the only writer of `run.status` and emits `run_completed` (`:85`) on every transition to `completed`. Per ruling 2 `run_failed`/`run_timeout` were NOT moved (they keep their run-level error context in `_execute`); the split and reasoning are at `docs/build-loop.md:333-378`, the vocabulary at `:321-330`. No event renamed, no line-shape key changed. Consumer audit: no notify/webhook code on this branch and no in-repo consumer infers run success from a task-status transition (Build Log renders events as they arrive; Dashboard counts `task.status`, a task verdict) — nothing to migrate; T-9's trigger set is written against these events at `build-loop.md:361-370`. Proof: integration `:447` `::test_normal_completion_is_a_terminal_event_and_needs_no_recovery`. Contract question filed as CCR-T8-01 (PROPOSED); `docs/frozen-contracts.md` untouched.

### 7. [x] DoD 7 — a retry after `run_timeout` leaves no contradictory lifecycle fields; decided, documented, tested, and checked against the GUI header's payload.

Reset in `ba77a43`; the GUI-payload assertion added in `05011a4`. Decided set = `task_service.py:137` `ROUND_FIELDS = (handoff_status, review_status, verification_status, merge_status)`; `:145` `clear_round_fields` clears only what is set and logs `retry_fields_reset`; called from `run_service.py:255` at the START of a round and deliberately NOT on a requeue (a queued task must keep showing the `CHANGES_REQUESTED` verdict the Worker acts on); `status` stays the frozen state machine's business. Documented at `docs/build-loop.md:122-136`. Proofs: unit `:407` (RUN_TIMEOUT + CHANGES_REQUESTED + fail + stale → cleared, logged, no empty churn); integration `:191` snapshots the header WHILE the new round is live — `status=in_progress` with `handoff_status=None` on the ORM row AND on the `_detail` payload the header renders; integration `:463` (requeue keeps the verdict, new round clears it).

### 8. [x] DoD 8 — unauthorized contract edits detected deterministically by the harness, with a structured build-log finding, a harness-detected reviewer-payload entry, and a decided route.

In `ba77a43`; REWRITTEN by ruling 1 (`21f79c4`), split per case in round 6. `project_service.py:29-45` `CONTRACT_FILES` = the five docs `contract_files()` knows; `:140` `_touched_paths` = branch diff **+ worktree dirt** (tracked AND untracked, so an uncommitted edit cannot hide); `:107-132` `authorized_contract_files` is a **GRANT test, not a mention test** (`:53-67` `_GRANT_RE` needs a change verb; `:68-79` `_PROHIBIT_RE` vetoes any negation; sources = `definition_of_done` + `acceptance_criteria` + `frozen_contracts` ONLY, never description, commit message or diffstat); `:47-51,134-138,155-175` set ops + `contract_findings`. `supervisor_service.py:46,211-262` fails `CONTRACT_CHECK` and emits `contract_change_detected` with `findings_json` naming the recovery commits; `:353-393,414` `contract_change_hold` — DECISION: straight to `blocked` + `review_status=NEEDS_HUMAN`, no review round burned, because a requeue would ask the Worker to fix what it may not fix (`docs/build-loop.md:137-168`); `review_service.py:114-137` gives the reviewer the same computed numbers. PER CASE (each name printed with its PASS line in `ruling1_grant_tests.txt`): (a) prohibition still flags → `test_recovery_markers.py:235-276`; (b) explicit grant suppresses → `:278-322`; (c) commit message / diffstat / description grant nothing → `:324-351`; (d) bare mention → `:353-365`. DoD's required shape: integration `:365-437` — a `docs/frozen-contracts.md` edit swept INTO a recovery commit, tests green, still fails → `blocked`/`NEEDS_HUMAN`/`recovery_commit: true`; `:417-425` a mention does not release the hold, `:429-437` a grant does. This task's own `docs/frozen-contracts.md`: 0 commits, 0 dirty.

### 9. [x] DoD 9 — the dirty predicate split by decision (merge/doctor on TRACKED dirt, recovery still sweeping untracked output) and tested as a split.

In `ba77a43`, verified by reading. `utils/git.py:70` `status_porcelain(tracked_only=)` (`-uall`, `-uno` when tracked_only) + `:87` `status_entries` (NUL-safe XY); `git_service.py:60` `DirtyStatus`, `:212` `status_split`, `:221` `is_dirty` (inclusive, meaning unchanged), `:228` `tracked_dirty`. `merge_no_ff:475` refuses on TRACKED dirt only and names the untracked half; `project_service.doctor:279` keys 'main checkout clean' on tracked dirt and adds a separate always-ok check listing untracked paths; `recovery_commit_info:291` STILL sweeps untracked worker output and reports `tracked_modified` vs `untracked_added` separately in the commit body AND the event payload; `remove_worktree:519` keeps the inclusive predicate so nothing is discarded. Proofs (a)-(d): `test_recovery_markers.py:124` (predicates), `:285` (doctor ok=true with untracked listed separately, then ok=false on a real tracked edit), `:445` (tracked dirt still blocks the merge; untracked-only worktree still swept), `:143` (cleanup still refuses to discard untracked output), `tests/integration/test_git_worktree.py::test_merge_gate_refuses_tracked_dirt_but_not_untracked`. No control widened: subject prefix, event names, `.managed/<task_id>` and never-delete-branches stand.

### 10. [x] DoD 10 — credential patterns anchored with the negative lookbehind `(?<![A-Za-z0-9])` (NOT a word boundary): names and `findings_json` payloads survive, credentials still redact.

Worker-authored (`9001e82`, sharpened in `94db0cc`) — NOT recovery wreckage. `utils/redaction.py:22-23` are now `(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{10,}` and `(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}`, with the measured damage beside them. The lookbehind, not `\b`, because `_` is a word character: `\bsk-` would miss `TOKEN_sk-proj-…` / `API_KEY_sk-ant-…` and so would have weakened redaction while fixing the names — the operator's original 'word boundary' wording is corrected here, in `docs/build-loop.md:364-378` and in every committed note, so nothing argues for `\b` any more. It matters because `log_service.py:28` redacts EVERY event's `details`: build_log_entries.id=86 handed the operator a merge command whose branch name was destroyed, and this task's `findings_json` payload loses a value while still parsing. Proofs: `tests/unit/test_redaction.py` (14 tests, measured at the capture HEAD) incl. `::test_branch_task_and_phase_names_survive_redaction`, `::test_findings_json_payload_survives_redaction`, `::test_credentials_are_still_redacted_wherever_they_appear`, `::test_build_log_details_keep_operational_commands_copy_pasteable`, `::test_underscore_joined_other_credentials_are_a_recorded_gap_not_a_regression`; plus `dod10_redaction_probe.txt`, which runs OLD (at `14a221c`) / BOUND (`\b`) / SHIPPED over the same strings — the `merge_candidate` command is whole only under SHIPPED, and the underscore-glued key leaks only under BOUND. Trade stated, not hidden: a credential glued to a letter (`xsk-proj-…`) is no longer caught; no anchor can both spare `task-` and catch that.

## Tests — every number stamped at one HEAD

```
$ ./scripts/orch_verify.sh test
421 passed, 0 failed, 6 skipped   (SUITE_EXIT=0)
$ ./scripts/orch_verify.sh lint
All checks passed!   (LINT_EXIT=0)
$ ./scripts/orch_verify.sh test tests/unit/test_recovery_markers.py
16 passed in 0.97s
$ ./scripts/orch_verify.sh test tests/integration/test_recovery_after_timeout.py
5 passed in 9.77s
$ ./scripts/orch_verify.sh test tests/unit/test_recovery_markers.py tests/integration/test_recovery_after_timeout.py
21 passed in 10.49s
$ ./scripts/orch_verify.sh test tests/unit/test_redaction.py
14 passed in 1.15s
$ ./scripts/orch_verify.sh test tests/integration/test_git_worktree.py
5 passed in 0.20s
$ ./scripts/orch_verify.sh test tests/unit/test_recovery_markers.py tests/integration/test_recovery_after_timeout.py tests/unit/test_redaction.py tests/integration/test_git_worktree.py
40 passed in 11.56s
```

Counts above are PARSED from the committed logs, not typed. Each `verify-evidence/T-8/*.txt` begins with `# HEAD=<sha> … captured=<iso8601>`, written by the script beside it (`suite_and_lint.sh`, `targeted_tests.sh`, `ruling1_grant_tests.sh`, `dod1_recovery_machinery.sh`, `dod10_redaction_probe.sh`, `dod4_gui_typecheck.sh`), so "one run at one HEAD" is checkable instead of asserted. The capture is `bd9239f` (first line of the log: `# HEAD=bd9239f0990751259fa2e924ce33c27c2b5a9882 (bd9239f) branch=task-8-recovery-commits-after-a-timeout-commit-half-fin captured=2026-11-04T02:17:04+08:00 tree=6 dirty-entries`); this block renders on `f3da9cb`, and every commit between them touches only `verify-evidence/` — checked by this script (`git diff --name-only bd9239f..HEAD` filtered to that directory, asserted empty), so the counts describe the tree under review. Three full-suite invocations ran this round — `00c14ec` 421/0/6, `4ca2e3b` 421/0/6, `bd9239f` 421/0/6 — identical, and the two commits between them added only evidence scripts. Round 5's reviewer finding 5 was exactly this claim made without the stamp (targeted at `21f79c4`, suite/lint at `e0904bb`); the stamp is the fix, and it is why the +4 since round 5's 417 reconciles: `test_recovery_markers.py` 12 → 16 (three split grant cases + one reviewer-payload test).

`./scripts/orch_verify.sh` prints `ORCH_VERIFY|mode=test|module_file=…/worktrees/coding-orchestrator/8/src/…` and exits 3 if the import resolves outside this worktree — so the numbers describe **this branch**, not the main checkout (the editable-install trap; `docs/build-loop.md` § "Verification commands must be path-independent"). GUI: `tsc -b --force` over this branch's `web/src` → exit 0. All tests offline: mock harness, mock provider, temp git repos; no live service, no network, nothing installed.

## `reviews.6` findings → what changed

| # | finding | resolution | where |
| --- | --- | --- | --- |
| 1 | CCR-T8-02 said `author` is an object; the server emits a string | CCR rewritten to the emitted shape with a key-by-key table; splitting author listed as a rejected option | `docs/contract-change-request-T8-02-recovery-commits-field.md` (codes it cites: `git_service.py:117,380`, `web/src/api/types.ts:89`) |
| 2 | `runs/<id>/review_payload.md` added to the frozen artifact layout, no DoD grant, overlaps the tracked archiving item | **removed**; layout untouched; the gap documented with one owner; coverage moved to section tests | `review_service.py:162-166`, `docs/build-loop.md:297-314`, `tests/unit/test_recovery_markers.py:367`, `tests/integration/test_recovery_after_timeout.py:311-363` |
| 3 | memory suggestion still called authorization 'a literal mention' | reworded to the grant rule, in both `project/decisions` and `project/gotchas` so they agree | `memory_suggestions` in `.orchestrator/handoff.json` |
| 4 | DoD 4 evidence claimed a REST route was asserted | corrected: since ruling 3 the test asserts the route **404s** and that the detail field carries what the GUI renders | DoD 4 evidence, this block |
| 5 | counts claimed from ONE run were measured at two HEADs | every capture re-run at one HEAD by HEAD-stamped scripts; the block states the stamp | `verify-evidence/T-8/suite_and_lint.sh`, first line of each `.txt`, Tests section above |
| 6 | ruling-1 control and the three proofs not visible; no file:line per case | the 90-line test split into four named cases (+ the missing diffstat assertion) and an evidence file printing the control with line numbers and each PASS | `tests/unit/test_recovery_markers.py:235,278,324,353`, `verify-evidence/T-8/ruling1_grant_tests.txt` |
| 7 | supplied block truncated at DoD 5; DoD 6-10 and trailing fields not reviewable | **recorded, not fixed** (operator ruling: harness limitation; do not touch review plumbing). Mitigated by ordering: DoD self-check first, then verdicts, counts and findings, so the decision content lands inside the ~10.6 KB head budget of the ~16 KB the reviewer receives — see the size line at the end of this block | `docs/build-loop.md:297-314`, `verify-evidence/T-8/render_handoff.py` docstring |
| 8 | 'Three of them had a named event' mapped 3 events onto 2 statuses; stray line break | corrected to two of four terminal statuses; `cancelled` named as a `task_status` line; `run_error` labelled as not a terminal status; break closed | `docs/build-loop.md:333-378` |

## Provenance

**DoD 1-9's implementation content is not a Worker-authored commit on this branch.** It sits in `ba77a43`, a `chore(orchestrator-recovery): uncommitted worker output from R-10` commit (16 files, +1627/-46) created by the harness after run 10 was SIGTERMed at the 2700 s timeout with no `handoff.json`. That content was **never self-checked by the Worker that wrote it** — the defect this task is about. It is left exactly as it is: not re-implemented, rewritten, amended, rebased or re-split into neat feature commits. `5a0cd1a` (round 2's sweep, 9 files, all under `verify-evidence/`) is the same and is also left alone. Declared work: round 2 verified every item by reading file:line, implemented DoD 10 (`9001e82`, lookbehind in `94db0cc`) and closed gaps (`232b6d4`, `e42304a`, `05011a4`); round 3 was submit-only, no code (the discard defect that killed it is **T-10** — `run_service.py` untouched here); round 4 regenerated evidence; round 5 applied the operator's rulings (`21f79c4`); **round 6 applies reviews.6's findings** (`00c14ec` code/docs, `bd9239f` + this commit for evidence). Each item's evidence says which commit its content came from. Finding 2 is worth stating plainly: the undeclared `review_payload.md` was itself a defect inside that wreckage, found by a reviewer and not by the harness — the DoD 8 detector watches the five contract documents, not the artifact-layout paragraph of `frozen-contracts.md`.

## Summary

ROUND 6 — applies the round-5 review (`reviews.6`, CHANGES_REQUESTED, 8 findings) on top of the already-green branch; no recovery machinery was re-implemented. Four code/doc changes, in `00c14ec` + `bd9239f`: (1, finding 1) `docs/contract-change-request-T8-02-*.md` said `author` is an object — the server emits a string (`git_service.py:117`, `%an <%ae>` at :380; `web/src/api/types.ts:89 author: string`), so the CCR now states the emitted shape key-by-key and lists splitting author as a rejected option; (2, finding 2) the `runs/<run_id>/review_payload.md` artifact is REMOVED — it added a file to the frozen artifact layout with no DoD grant and overlapped the separately tracked review-payload-archiving item, which now has one owner (docs/build-loop.md:297-314 'Review input size (known limitation)'); (3, finding 6) the one 90-line ruling-1 test is split into four named cases so each proof is citable per case, and the diffstat assertion it lacked is added — `verify-evidence/T-8/ruling1_grant_tests.txt` prints the control with line numbers plus a -vv run naming each PASS; (4, finding 8) build-loop.md's 'Three of them had a named event' is corrected to two of four terminal statuses (run_error is not a status) and the stray 'It is\n**additive**' break is closed. Findings 3, 4, 5 and 7 are handoff-accuracy findings, fixed here: the memory suggestion no longer says 'literal mention', the DoD 4 evidence no longer claims a REST route is asserted (it asserts a 404), and every count in this handoff and in verify-evidence/T-8/ is stamped at ONE capture HEAD (bd9239f) by the script that produced it. Suite 421 passed / 0 failed / 6 skipped, lint clean, both via ./scripts/orch_verify.sh. ba77a43 and 5a0cd1a remain unamended, unrebased and un-split; DoD 1-9's implementation content still lives inside those recovery commits and says so per item. Not merged, not pushed.

## Files changed (37)

* `src/orchestrator/api/routes/tasks.py`
* `src/orchestrator/services/git_service.py`
* `src/orchestrator/services/project_service.py`
* `src/orchestrator/services/prompt_builder.py`
* `src/orchestrator/services/review_service.py`
* `src/orchestrator/services/run_service.py`
* `src/orchestrator/services/supervisor_service.py`
* `src/orchestrator/services/task_service.py`
* `src/orchestrator/utils/git.py`
* `src/orchestrator/utils/redaction.py`
* `src/orchestrator/utils/structured_log.py`
* `tests/integration/test_git_worktree.py`
* `tests/integration/test_recovery_after_timeout.py`
* `tests/unit/test_recovery_markers.py`
* `tests/unit/test_redaction.py`
* `docs/build-loop.md`
* `docs/contract-change-request-T8-02-recovery-commits-field.md`
* `docs/contract-change-request-T8-build-log-events.md`
* `web/src/api/types.ts`
* `web/src/pages/TaskDetail.tsx`
* `verify-evidence/T-8/README.md`
* `verify-evidence/T-8/dod10_redaction_probe.sh`
* `verify-evidence/T-8/dod10_redaction_probe.txt`
* `verify-evidence/T-8/dod1_recovery_machinery.sh`
* `verify-evidence/T-8/dod1_recovery_machinery.txt`
* `verify-evidence/T-8/dod4_gui_typecheck.sh`
* `verify-evidence/T-8/dod4_gui_typecheck.txt`
* `verify-evidence/T-8/full_suite.txt`
* `verify-evidence/T-8/handoff.md`
* `verify-evidence/T-8/lint.txt`
* `verify-evidence/T-8/render_handoff.py`
* `verify-evidence/T-8/ruling1_grant_tests.sh`
* `verify-evidence/T-8/ruling1_grant_tests.txt`
* `verify-evidence/T-8/suite_and_lint.sh`
* `verify-evidence/T-8/targeted_tests.sh`
* `verify-evidence/T-8/targeted_tests.txt`
* `verify-evidence/T-8/write_handoff.py`

## New dependencies

None. Nothing was installed; no network was used.

## Contracts touched

* docs/frozen-contracts.md: NOT edited. The two contract questions are filed as CCR-T8-01 (docs/contract-change-request-T8-build-log-events.md) and CCR-T8-02 (docs/contract-change-request-T8-02-recovery-commits-field.md), both status PROPOSED.
* Build-log line shape {timestamp, phase, task_id, run_id, role, event, details, commit_sha}: unchanged. The structured recovery payload rides INSIDE the existing `details` string.
* Existing build-log event names: unchanged, additive only. `run_completed` is the added terminal success name; `run_failed`/`run_timeout` stay emitted from `_execute` (ruling 2), split documented in docs/build-loop.md.
* `chore(orchestrator-recovery):` commit-subject prefix and the `recovery_commit` event name: preserved byte-exact, trailer added alongside.
* Frozen artifact directory layout runs/<run_id>/: UNCHANGED as of round 6 — the `review_payload.md` file that round-2 wreckage had added is removed, so this branch adds no artifact name to the layout.
* REST API shapes under /api: ONE additive field (`recovery_commits` on GET /api/tasks/{id}), declared in CCR-T8-02 (PROPOSED). No new route: the one added in round 2 was deleted in round 5 (ruling 3).
* Task/run state machine core/state.py: not modified. Merge-gate semantics: unchanged — only the dirtiness predicate it keys on was narrowed to tracked content (DoD 9), which is what makes the refusal and the doctor verdict correct for untracked-only checkouts.
* Cleanup contract: still only worktrees under worktree_root carrying .managed/<task_id>, and branches are never deleted. `remove_worktree` keeps the INCLUSIVE dirty predicate so no worker output is ever discarded.
* Contract-file authorization semantics (project_service.authorized_contract_files): narrowed from 'mentioned' to 'explicitly granted' — a security control tightened, never loosened (ruling 1).

## Assumptions

* DoD 1-9's implementation content is `ba77a43`'s — a `chore(orchestrator-recovery)` commit the harness created after run 10 was SIGTERMed with no handoff. It was verified by reading the named file:line and by running the committed tests, not re-authored, and not amended/rebased/re-split. Where an item was not literally satisfied by that content, the gap is closed in a separate declared commit and named in the item's evidence (DoD 5 lint half `05011a4`; DoD 6/7 docs `232b6d4`; DoD 7 GUI payload `05011a4`; DoD 8 grant semantics `21f79c4` + this round).
* Every committed number in this handoff was produced at ONE HEAD (f3da9cb) by `verify-evidence/T-8/suite_and_lint.sh`, `targeted_tests.sh`, `ruling1_grant_tests.sh`, `dod1_recovery_machinery.sh`, `dod10_redaction_probe.sh` and `dod4_gui_typecheck.sh`; each .txt carries `HEAD=<sha>` in its first line. `src/`, `tests/`, `web/` and `docs/` were clean at capture (`git status --porcelain -- src tests scripts web docs` empty); the only dirty entries were the evidence .txt files being rewritten by the same run. Three full-suite invocations happened this round: one pre-capture check at `00c14ec` (421/0/6), the captured run at `4ca2e3b` (421/0/6), and the committed run at `bd9239f` (421/0/6) — same counts at all three, and the two intervening commits added only files under `verify-evidence/`.
* Ruling 2 is taken as settling the 'same chokepoint' question earlier rounds filed: `run_completed` stays in `_set_run`, `run_failed`/`run_timeout` stay in `_execute`, and the docs state the split. No emitter moved, so no log volume or ordering changed.
* Ruling 3 is taken as settling DoD 4's API surface: `recovery_commits` on the task detail response is the only serving surface; there is no endpoint, and the integration test asserts the 404.
* Ruling 4 is implemented as a CCR, not a contract edit: `docs/frozen-contracts.md` is untouched (`0` commits, `0` dirty — see `ruling1_grant_tests.txt`). CCR-T8-02's field description now matches the emitted types exactly, including `author: str` formatted `%an <%ae>`.
* Recovery-commit detection is trailer-only, so pre-trailer recovery commits (`ba77a43`, `5a0cd1a`) are NOT flagged by the machinery and cannot be without the amendment that was refused. Documented as the 'Boundary' in `docs/build-loop.md:114-121` and stated in this handoff instead.
* Ruling 1's grant vocabulary is a heuristic over English DoD text; it FAILS CLOSED — an unrecognised grant phrasing raises a finding and routes to NEEDS_HUMAN for a human. The direction of the residual risk is deliberate: a grant that should authorize and does not is a nuisance, the reverse was the bug.
* Review-input truncation is a HARNESS limitation, recorded not fixed here (round-5 operator ruling on finding 6; round-6 reviewer finding 7 repeats it). This round therefore changed no review/verification plumbing — in fact it REMOVED the one unauthorized artifact that round-2 wreckage had added — and mitigates by keeping this block short, pointing at committed evidence, and putting the decision-critical fields near the top and bottom rather than the middle.
* The archived copy of this handoff will render the branch name mangled (`ta[REDACTED]…`) because `artifact_service.write_text` redacts and the running harness is `main@360228a`, which does not yet carry DoD 10's lookbehind. The on-disk `.orchestrator/handoff.json` in the worktree is byte-exact; the fix is already on this branch.

## Questions

1. CCR-T8-01 (docs/contract-change-request-T8-build-log-events.md, PROPOSED): freeze `run_completed` and/or the ` | findings_json=` marker in docs/frozen-contracts.md? This branch would pick option 3 (freeze only the event name), because the marker rides inside a frozen string and is the easier part to break by accident.
2. CCR-T8-02 (docs/contract-change-request-T8-02-recovery-commits-field.md, PROPOSED): freeze the additive `recovery_commits` field on GET /api/tasks/{id}? Safest option 1 (approve as written, zero code change); option 4 (split `author` into {name,email}) is now listed and NOT recommended, because it would break an emitted field the client already reads as a string.
3. DoD 4's 'GUI diff marks recovery commits distinctly' is a warning block above the whole-diff view plus a header badge, not per-hunk attribution, because patch.diff is one flat diff against the base branch. Accepted as-is by rulings 3+4; per-commit hunks would be an artifact-format change beyond this phase — confirm that boundary is right.
4. Should pre-trailer recovery commits (ba77a43, 5a0cd1a) get an explicit 'legacy recovery commit' annotation in the reviewer payload, or is the documented Boundary enough? The annotation would be a subject-based fallback, which is exactly what the trailer rule rejects.
5. Ruling 1's grant test reads task.frozen_contracts as an authorization source (the ruling names it). A frozen-contracts entry that merely QUOTES a contract path — which is what this project's frozen list does — grants nothing. Confirm that reading before a future phase relies on it.
6. Review-input size policy (finding 7 / the separately tracked item): should the reviewer payload get an explicit budget with a 'N of M files shown' header, or should the full payload be archived by hash — which needs a CCR for the frozen runs/<run_id>/ layout? T-8 deliberately did neither.

## Blockers

None.

## Evidence paths (committed; none under gitignored `.orchestrator/`)

* `verify-evidence/T-8/README.md` — the DoD → file:line → proof index, incl. per-case citations for ruling 1 and the round-6 finding-by-finding note
* `full_suite.txt`, `lint.txt`, `targeted_tests.txt`, `ruling1_grant_tests.txt`, `dod1_recovery_machinery.txt`, `dod10_redaction_probe.txt`, `dod4_gui_typecheck.txt` — each with the `.sh` that regenerates it, each HEAD-stamped
* `write_handoff.py` (writes + schema-validates `handoff.json`) and `render_handoff.py` (this block)
* `docs/contract-change-request-T8-build-log-events.md` (CCR-T8-01) and `docs/contract-change-request-T8-02-recovery-commits-field.md` (CCR-T8-02) — both PROPOSED, neither applied

## Memory suggestions (7 — full text in `.orchestrator/handoff.json`, which the harness reads machine-side)

* **project/gotchas** — coding-orchestrator: authorization checks over natural-language DoD text must be GRANT tests, not mention tests. project_service.authorized_contract_files used to match a contract path anywhere in definition_of_done/acceptance_cri…
* **project/gotchas** — In coding-orchestrator, utils/redaction.py's sk- credential patterns were unanchored until T-8: `sk-(?:proj-)?[A-Za-z0-9_-]{20,}` matched INSIDE words ending in 'sk', and because log_service.log_event() redacts every event's `deta…
* **project/conventions** — coding-orchestrator now has two dirty predicates and the split is deliberate: git_service.tracked_dirty() (`git status --porcelain -uall -uno`) answers 'did versioned content change?' and gates merge_no_ff's refusal and project do…
* **project/decisions** — T-8 decisions recorded in docs/build-loop.md: (1) a recovery commit is machine-identifiable by the git trailer `Orchestrator-Recovery: <run_short_id>` and NEVER by subject string matching (a hand-authored commit can copy the subje…
* **role/worker** — On this harness a Worker round that times out can still land a complete, lint-clean, test-green implementation — as UNDECLARED content inside a chore(orchestrator-recovery) commit (measured: ba77a43, 16 files +1627/-46, independen…
* **global/environment** — To type-check a git worktree of Coding-Orchestration-Agent-Harness that has no web/node_modules: build a scratch dir under /tmp, symlink <main>/web/node_modules into it and symlink the worktree's web/{src,index.html,package.json,t…
* **project/conventions** — Evidence for a gated run in coding-orchestrator must be committed under a real, reviewer-reachable path: `verify-evidence/<task>/` with `.txt` outputs and a README that maps each DoD item to file:line and to the log that proves it…

## Out of scope, and not touched

`src/orchestrator/adapters/**`, pi chat drivers, `tests/unit/test_chat_drivers.py`, `context_windows`, `chat_service.py`, `run_service.py`'s ingestion path (T-10 owns the discard-on-timeout defect), `docs/frozen-contracts.md`, merge-gate semantics, `core/state.py`, review/verification plumbing. Divergence is noted, not resolved: `main@360228a` moved twice since this branch was cut; `git merge-tree` reports no conflicts. `git diff main..HEAD` shows phantom deletions in `test_chat_drivers.py` that do not exist — diff against the merge-base `14a221c`, never against `main`.

**Block size, computed by the renderer at write time:** the handoff string the Architect receives is this file, then a `handoff.json:` separator, then the JSON string (`review_service.py:151-153`), middle-truncated to `max_log_tokens=4000` ≈ 16 000 chars: ~10.6 KB of head, ~5.3 KB of tail (`utils/truncate.py:13-19`). This block and the JSON are sized as follows: **this block = 35,900 bytes**, `handoff.json` = 31,159 bytes (round 5's block was 48 621 bytes). The order is: header, verdict digest, DoD detail, counts, findings. The head budget therefore carries the digest (all 10 verdicts, the test counts and the dependency/blocker tallies) and roughly DoD 1-7 of the detail; the rest falls past the seam, with each remaining verdict already on a line of the digest. That is a mitigation, not a fix — the fix belongs to the harness item recorded in finding 7.
