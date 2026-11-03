# T-8 verification evidence — recovery commits after a timeout

Task **T-8** · branch `task-8-recovery-commits-after-a-timeout-commit-half-fin` · project
**Coding Orchestrator** (base `main`).

This directory exists for the same reason `verify-evidence/T-6/` does: `.orchestrator/` is
gitignored and nothing under it is archived into `runs/<run_id>/`, so evidence written there is
invisible to the review gate. Outputs use `.txt` because `*.log` is gitignored. Scripts are
committed next to their output so every number here is re-runnable with
`bash verify-evidence/T-8/<name>.sh`.

## Provenance, stated up front

**DoD 1–9 landed in `ba77a43`, which is a `chore(orchestrator-recovery): uncommitted worker
output from R-10` commit.** That is exactly the artifact class this task exists to label: run 10
was SIGTERMed at the 2700 s adapter timeout with no `handoff.json`, so the Worker never
self-checked any of it, and the harness committed the wreckage under the orchestrator git
identity. Round 2 (this round) did **not** re-implement or re-author that content and did not
rewrite, amend, rebase or split `ba77a43` — laundering wreckage into apparent intentional work
would be the very defect T-8 fixes. What round 2 did:

* **verified each of DoD 1–9 by reading the shipped file and line** (table below),
* **implemented the one item `ba77a43` did not** — DoD 10, the redaction anchor (`9001e82`),
* **closed three gaps found while verifying** — the `docs/build-loop.md` sections that were
  referenced but never written and the missing `run_completed` entry in the event vocabulary
  (`232b6d4`), the LINT-command half of DoD 5 and the GUI-header half of DoD 7 (`05011a4`),
  and the recovery-detection boundary note (`05011a4`).

## DoD item → where it is, and what proves it

| DoD | Implementation (file:line) | Proof |
| --- | --- | --- |
| 1 — recovery commits machine-identifiable: trailer + structured field in the existing `recovery_commit` event | `services/git_service.py:46-49` (`RECOVERY_SUBJECT_PREFIX`, `RECOVERY_TRAILER_KEY`, `_RECOVERY_TRAILER`), `:82-99` (`RecoveryCommit`, `.trailer`), `:291-333` (`recovery_commit_info`), `:335-352` (`recovery_event_details` / `parse_recovery_event_details`), `:383-455` (`commits_against_base` sets `recovery` **from the trailer only**), `utils/structured_log.py` (`\| findings_json=` inside the frozen `details` string) | `dod1_recovery_machinery.txt` §§ (a),(b): a fresh sweep produces subject `chore(orchestrator-recovery): …` **and** trailer `Orchestrator-Recovery: R-demo`, and the event `details` parse back to `{recovery: true, run_short_id, sha, tracked_modified, untracked_added, trailer, skipped_sensitive}`. Tests: `tests/unit/test_recovery_markers.py::test_recovery_commit_keeps_subject_prefix_and_adds_trailer`, `::test_recovery_detected_by_trailer_not_by_subject` (a hand-authored commit *copying the subject* is **not** detected), `::test_recovery_event_details_are_machine_readable` |
| 2 — subject prefix kept byte-exact, trailer added alongside | `git_service.py:247-273` (`recovery_message`: prefix line first, trailer last) | `dod1_recovery_machinery.txt` § (b) prints the whole commit body; test asserts `subject == "chore(orchestrator-recovery): uncommitted worker output from R-1"` |
| 3 — the next run is told, with the commit list and a mandatory re-verify | `run_service.py:321-347` (builds `recovery_state`, logs `recovery_notice_included`), `prompt_builder.py:126-141` (`DynamicState.recovery` section, cache-dynamic tier) | `tests/integration/test_recovery_after_timeout.py::test_timeout_wreckage_is_committed_as_recovery_and_told_to_the_next_run` asserts the section heading, `NEVER self-checked`, "re-verify the branch with the project's lint and test commands", the swept sha in `runs/<id>/prompt.md`, and that round 3 is told too. `dod1_recovery_machinery.txt` § (b) prints `format_recovery_commit_list` |
| 4 — reviewer payload + GUI diff mark recovery, review prompt says it was never self-checked | `review_service.py:95-110` (`recovery_section`), `:114-136` (`harness_findings_section`), `:156-165` (both composed into the prompt and stored as `runs/<id>/review_payload.md`); `supervisor_service.py:47,270-285` (`RECOVERY_CHECK`, informational, lists the commits + "content was NEVER self-checked by the Worker"); API `api/routes/tasks.py:56-63` (`recovery_commits` in the task payload), `:119-133` (`GET /api/tasks/{id}/recovery`); GUI `web/src/pages/TaskDetail.tsx:29-56` (`RecoveryNote` above the diff), `:95` (header badge), `web/src/api/types.ts:85-102` | `::test_recovery_reaches_the_reviewer_and_the_gui_payload` (payload text, `review_payload.md`, the REST route, the task detail); `dod4_gui_typecheck.txt` (this branch's `web/src` type-checks, 0 errors, sources resolved from the worktree) |
| 5 — offline tests for the whole shape, including a broken intermediate state surfaced by verification | `tests/unit/test_recovery_markers.py` (265 lines), `tests/integration/test_recovery_after_timeout.py` (456 lines) | `full_suite.txt`. The timeout run leaves `def handle(): return not_yet_defined(1)`; round 2's mock Worker claims `READY_FOR_REVIEW` + `met: true`, and the gate fails it: `tests` **fail** (`test.log` names `not_yet_defined`) **and** `lint` **fail** (`lint.log` = `app.py:2: F821 undefined name 'not_yet_defined'`), no reviewer is consulted. The lint-command half was **added in round 2** (`05011a4`): `ba77a43` only had the runtime/test path, and run 5's actual defect was an F821 |
| 6 — terminal success event, same chokepoint, documented, additive only | `run_service.py:70-90` (`_set_run`, the only writer of `run.status`, emits `run_completed`); `docs/build-loop.md:282-293` vocabulary now lists it and `:294-326` ("Terminal run events") names it next to `run_failed`/`run_timeout`/`run_error`, records that `_set_run` is where the failure events originate, that no in-repo consumer had to migrate, and expresses the **T-9** notification trigger set against it | `::test_normal_completion_is_a_terminal_event_and_needs_no_recovery` (event present, `run_failed`/`run_timeout` absent, run/task ids attached, exit code in the summary). Line shape `{timestamp, phase, task_id, run_id, role, event, details, commit_sha}` untouched; no existing event renamed |
| 7 — a retry leaves no contradictory lifecycle fields | `task_service.py:131-161` (`ROUND_FIELDS` = `handoff_status`, `review_status`, `verification_status`, `merge_status`; `clear_round_fields` logs `retry_fields_reset`), called from `run_service.py:253-255` at the start of the round; documented in `docs/build-loop.md:116-130` § "What a new round resets" | `::test_timeout_wreckage…` snapshots the header **while the new round is live** and asserts `status=in_progress` with `handoff_status=None` — and, since `05011a4`, asserts the same on the `routes/tasks.py:_detail` payload the GUI header actually renders; `::test_new_round_resets_contradictory_lifecycle_fields` (unit), `::test_retry_keeps_the_verdict_to_act_on_then_resets_it_for_the_new_round` (a requeue deliberately keeps `CHANGES_REQUESTED`, so feedback is not thrown away). `dod4_gui_typecheck.txt` shows the header fields exist in the typed payload |
| 8 — unauthorized contract edits detected by the harness | `project_service.py:29-105` (`CONTRACT_FILES`, `contract_files()`, `authorized_contract_files()`, `_touched_paths()` = branch diff **+ worktree dirt**, `contract_findings()`), `supervisor_service.py:46` (`CONTRACT_CHECK`), `:211-262` (failing check + `contract_change_detected` with a `findings_json` payload naming the recovery commits), `:353-390` (`contract_change_hold` → `review_status=NEEDS_HUMAN` + `blocked`), `review_service.py:114-136` (harness-detected entry in the reviewer payload); documented in `docs/build-loop.md:131-150` § "Unauthorized contract edits" incl. the **decision to route straight to NEEDS_HUMAN** instead of burning a review round | `::test_unauthorized_contract_edit_from_recovery_waits_for_a_human` (tests green, contract hit inside a recovery commit → verification fail, `blocked`, `NEEDS_HUMAN`, no reviewer, structured event, and an authorizing DoD item releases it), `::test_unauthorized_contract_change_is_computed_not_asserted` (unit, authorized vs unauthorized from the stored DoD) |
| 9 — dirty predicate split by decision, tested | `utils/git.py:70-104` (`status_porcelain(tracked_only=)` with `-uall`, `status_entries()` NUL-safe XY parsing), `git_service.py:59-79` (`DirtyStatus`), `:212-236` (`status_split`, `is_dirty`, `tracked_dirty`), `merge_no_ff:475-497` keys on **tracked** dirt and names the untracked half in the message, `project_service.py:209-246` (doctor: `main checkout clean` on tracked dirt only, plus a separate always-ok check listing untracked paths), `recovery_commit_info:291-333` keeps sweeping untracked output and reports it in `untracked_added`, `remove_worktree:519-527` keeps the inclusive predicate | `dod1_recovery_machinery.txt` § (b) tail: an untracked-only worktree gives `is_dirty=True`, `tracked_dirty=False`, and is still swept with `tracked_modified: []`. Tests: `::test_dirty_predicates_separate_tracked_from_untracked`, `::test_merge_refusal_for_tracked_dirt_and_recovery_still_captures_untracked`, `::test_cleanup_still_refuses_untracked_output`, `::test_doctor_reports_untracked_dirt_without_failing_cleanliness`, `tests/integration/test_git_worktree.py::test_merge_gate_refuses_tracked_dirt_but_not_untracked` (a)–(d) |
| 10 — credential patterns anchored so operational text survives | `utils/redaction.py:11-20` (`_PATTERNS`, with `\bsk-ant-…` at :19 and `\bsk-(?:proj-)?…` at :20, and the reason written next to them) | `dod10_redaction_probe.txt` runs the **old** unanchored patterns and the shipped `redact()` over the same strings: `task-8-…`/`task-6-…`/`phase-3-risk-…-mask-…`/a `findings_json` payload were damaged before and are byte-identical now, while `sk-ant-`, `sk-proj-`, bare `sk-`, `gh[pousr]_`, `github_pat_`, `glpat-`, `xoxb-`, `AKIA…`, `AIza…` and a JWT still redact in bare / assigned / quoted / word-glued / JSON-embedded positions. Tests: `tests/unit/test_redaction.py::test_branch_task_and_phase_names_survive_redaction`, `::test_findings_json_payload_survives_redaction`, `::test_credentials_are_still_redacted_wherever_they_appear`, `::test_build_log_details_keep_operational_commands_copy_pasteable` |

## Logs

Captured at HEAD `e7953b5`. The only commit after that one changes files under `verify-evidence/`
and `.orchestrator/` — neither is read by the suite — so the code under test is identical to the
final HEAD of this handoff.

| File | What it is |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test`: `ORCH_VERIFY\|mode=test\|module_file=…/worktrees/coding-orchestrator/8/src/…` (asserted to resolve **inside this worktree**, not the main checkout) then **415 passed, 6 skipped**, `TEST_EXIT=0` |
| `lint.txt` | `./scripts/orch_verify.sh lint`: `ruff check src tests` → `All checks passed!`, `LINT_EXIT=0` |
| `dod1_recovery_machinery.sh` / `.txt` | the shipped detector run over real git objects: this branch's own history (where `ba77a43` is **not** flagged, because it predates the trailer — see the boundary note in `docs/build-loop.md`), a fresh simulated sweep, and the two dirty predicates |
| `dod10_redaction_probe.sh` / `.txt` | old-vs-new redaction over the same strings, plus the real `merge_candidate` line shape |
| `dod4_gui_typecheck.sh` / `.txt` | `tsc -b --force` over this branch's `web/src` (toolchain taken from the main checkout because a worktree has no `node_modules`), exit 0, with the `--listFiles` proof that the sources read are the worktree's, and the grep of the recovery surfaces the GUI renders |
| `targeted_tests.txt` | the three T-8 test files run on their own: `16 passed` (`test_recovery_after_timeout.py` + `test_recovery_markers.py`) and `13 passed` (`test_redaction.py`) |

## What is NOT claimed here

* `ba77a43` is not flagged as a recovery commit by the trailer machinery — it predates it. The
  detector is trailer-only on purpose (`docs/build-loop.md:108-115`, "Boundary"); the honest statement is
  in this file and in `handoff.md`, not a silent flag.
* No live service, provider, adapter or GUI was exercised. Everything above is offline, on temp
  git repos, with the mock harness and the mock provider.
* `docs/frozen-contracts.md` is untouched on this branch. `run_completed` and the structured
  `details` payload are documented in `build-loop.md` only; if the reviewer wants contract
  language for them, that is a human decision (T-6's pattern:
  `docs/contract-change-request-T6-orch-verify-marker.md`, status PROPOSED).
* Divergence is noted, not resolved: `main` is at `360228a` (Merge PR #17 `fix/pi-compaction`)
  and this branch has moved ahead since; `git merge-tree` still reports no conflicts. Nothing
  under `adapters/chat/*`, `pi.py`, `pi_rpc.py`, `chat_service.py`, `test_chat_drivers.py` or
  `context_windows` was touched here.
