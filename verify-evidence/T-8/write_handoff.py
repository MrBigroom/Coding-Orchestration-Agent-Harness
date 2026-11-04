"""Machine-readable handoff for T-8 round 6 — written as a script so every number in it is
computed from the tree, not typed from memory.

    python verify-evidence/T-8/write_handoff.py        # -> .orchestrator/handoff.json

Counts come from the committed capture files (verify-evidence/T-8/*.txt), which are themselves
HEAD-stamped by the scripts that wrote them. If a committed log disagrees with this file, the log
is right and this file is stale.
"""

from __future__ import annotations

import json
import re
import subprocess

from orchestrator.core.schemas import Handoff

MERGE_BASE = "14a221c"  # merge-base(main, HEAD) at the time this round was cut


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def read(path: str) -> str:
    return open(path).read()


def count(pattern: str, path: str) -> int:
    m = re.search(pattern, read(path))
    if not m:
        raise AssertionError(f"{pattern!r} not found in {path}")
    return int(m.group(1))


HEAD = git("rev-parse", "--short", "HEAD")
FULL_HEAD = git("rev-parse", "HEAD")
BEHIND, AHEAD = (git("rev-list", "--left-right", "--count", "main...HEAD").split() + ["?", "?"])[:2]
MAIN = git("rev-parse", "--short", "main")

SUITE_PASSED = count(r"(\d+) passed", "verify-evidence/T-8/full_suite.txt")
SUITE_SKIPPED = count(r"(\d+) skipped", "verify-evidence/T-8/full_suite.txt")
SUITE_FAILED = 0  # full_suite.txt: SUITE_EXIT=0, no "failed" in the summary line
CAPTURED_HEAD = re.search(r"HEAD=([0-9a-f]{40})", read("verify-evidence/T-8/full_suite.txt")).group(1)
# Evidence commits land AFTER the capture, so the rule is not "same HEAD" but "nothing but evidence
# changed since the capture". Anything else in the diff means the committed counts are stale:
# re-run  bash verify-evidence/T-8/suite_and_lint.sh
_since = [
    f
    for f in git("diff", "--name-only", f"{CAPTURED_HEAD}..HEAD").splitlines()
    if not f.startswith("verify-evidence/")
]
assert not _since, f"non-evidence files changed since the suite capture {CAPTURED_HEAD[:7]}: {_since}"
assert "All checks passed!" in read("verify-evidence/T-8/lint.txt")
assert f"HEAD={CAPTURED_HEAD}" in read("verify-evidence/T-8/lint.txt")
assert f"HEAD={CAPTURED_HEAD}" in read("verify-evidence/T-8/targeted_tests.txt")
assert f"HEAD={CAPTURED_HEAD}" in read("verify-evidence/T-8/ruling1_grant_tests.txt")

FILES = git("diff", "--name-only", f"{MERGE_BASE}..HEAD").splitlines()

SUMMARY = (
    f"ROUND 6 — applies the round-5 review (`reviews.6`, CHANGES_REQUESTED, 8 findings) on top of "
    f"the already-green branch; no recovery machinery was re-implemented. Four code/doc changes, in "
    f"`00c14ec` + `bd9239f`: (1, finding 1) `docs/contract-change-request-T8-02-*.md` said `author` "
    f"is an object — the server emits a string (`git_service.py:117`, `%an <%ae>` at :380; "
    f"`web/src/api/types.ts:89 author: string`), so the CCR now states the emitted shape key-by-key "
    f"and lists splitting author as a rejected option; (2, finding 2) the `runs/<run_id>/review_payload.md` "
    f"artifact is REMOVED — it added a file to the frozen artifact layout with no DoD grant and "
    f"overlapped the separately tracked review-payload-archiving item, which now has one owner "
    f"(docs/build-loop.md:297-314 'Review input size (known limitation)'); (3, finding 6) the one "
    f"90-line ruling-1 test is split into four named cases so each proof is citable per case, and the "
    f"diffstat assertion it lacked is added — `verify-evidence/T-8/ruling1_grant_tests.txt` prints the "
    f"control with line numbers plus a -vv run naming each PASS; (4, finding 8) build-loop.md's "
    f"'Three of them had a named event' is corrected to two of four terminal statuses (run_error is not "
    f"a status) and the stray 'It is\\n**additive**' break is closed. Findings 3, 4, 5 and 7 are "
    f"handoff-accuracy findings, fixed here: the memory suggestion no longer says 'literal mention', the "
    f"DoD 4 evidence no longer claims a REST route is asserted (it asserts a 404), and every count in "
    f"this handoff and in verify-evidence/T-8/ is stamped at ONE capture HEAD "
    f"({git('rev-parse', '--short', CAPTURED_HEAD)}) by the script that "
    f"produced it. Suite {SUITE_PASSED} passed / {SUITE_FAILED} failed / {SUITE_SKIPPED} skipped, lint "
    f"clean, both via ./scripts/orch_verify.sh. ba77a43 and 5a0cd1a remain unamended, unrebased and "
    f"un-split; DoD 1-9's implementation content still lives inside those recovery commits and says so "
    f"per item. Not merged, not pushed."
)

# Item text: a short, unambiguous restatement (the reviewer already has the verbatim DoD list in
# its own prompt; duplicating 3.5 KB of it here only pushed the decision content past the
# reviewer's max_log_tokens budget). Evidence: the file:line, the proof, the provenance — and the
# full detail lives in verify-evidence/T-8/README.md, which is committed.
DOD = [
    (
        "DoD 1 — recovery commits are machine-identifiable (trailer + structured event field), not "
        "subject-matched.",
        "Content arrived UNDECLARED in recovery commit `ba77a43`; verified by reading + running, not "
        "re-authored. `git_service.py:47` `RECOVERY_TRAILER_KEY`, `:49` `_RECOVERY_TRAILER`, `:82` "
        "`RecoveryCommit.trailer`, `:291` `recovery_commit_info`, `:335`/`:347` event payload + parser, "
        "`:383` `commits_against_base` sets `CommitInfo.recovery` (`:118`) from the TRAILER ONLY; "
        "`utils/structured_log.py:22` rides ` | findings_json={…}` inside the existing `details` string, "
        "so the frozen line shape is untouched. Proofs: `tests/unit/test_recovery_markers.py:33,58,97` "
        "(:58 = a hand-authored commit copying the subject is NOT detected), "
        "`dod1_recovery_machinery.txt` §§(a)(b).",
        True,
    ),
    (
        "DoD 2 — the `chore(orchestrator-recovery):` subject prefix is unchanged byte-exact; the "
        "trailer is added alongside.",
        "`git_service.py:46` `RECOVERY_SUBJECT_PREFIX = \"chore(orchestrator-recovery):\"`; "
        "`recovery_message` writes the prefix subject first and the trailer last. "
        "`test_recovery_markers.py:44` asserts the full subject string; `dod1_recovery_machinery.txt` "
        "§(b) prints a real commit body. Event name `recovery_commit` unchanged "
        "(`docs/build-loop.md:86`). `ba77a43`/`5a0cd1a` keep their original messages — not amended.",
        True,
    ),
    (
        "DoD 3 — the next run on a branch with recovery commits is told, with the list and a mandatory "
        "re-verify before handoff.json.",
        "In `ba77a43`, verified by reading. `run_service.py:324-343` builds `recovery_state` from "
        "`project_service.recovery_commits_for` + `git_service.format_recovery_commit_list:457` and logs "
        "`recovery_notice_included`; `prompt_builder.py:126-141` renders it in the cache-DYNAMIC tier. "
        "The text requires re-verifying with the project's lint/test commands, repairing or reporting "
        "every defect the swept commits introduced, and a BLOCKED handoff + contract-change request if a "
        "swept edit touched an unauthorized contract file. Proof: "
        "`tests/integration/test_recovery_after_timeout.py:191` asserts heading, `NEVER self-checked`, the "
        "re-verify instruction, the swept sha in `runs/<id>/prompt.md`, and that round 3 is told too.",
        True,
    ),
    (
        "DoD 4 — reviewer payload and GUI mark recovery distinctly; the prompt says plainly that "
        "recovery content was never self-checked.",
        "In `ba77a43`, amended round 6 (finding 2). `review_service.py:95-112` `recovery_section` "
        "('created by the ORCHESTRATOR … NEVER self-checked') + `:114-137` `harness_findings_section`, "
        "rendered by `prompt_builder.py:126-144` as 'Recovery commits on this branch (created by the "
        "harness, NEVER self-checked by the Worker)' / 'Harness-detected findings'; "
        "`supervisor_service.py:47,271-287` `RECOVERY_CHECK`; GUI `TaskDetail.tsx:29-56` + `:95` typed "
        "`types.ts:85-93,101-102`, served ONLY by `api/routes/tasks.py:38,62-69`. ROUND 6 (finding 2): the "
        "`runs/<id>/review_payload.md` writer is DELETED (`review_service.py:162-166` says why) — it added "
        "a file to the FROZEN artifact layout with no DoD grant; `docs/build-loop.md:297-314` documents the "
        "gap and its one owner. Proofs: integration `:311-363` (both sections, the swept path, NO "
        "`review_payload.md` in the run dir, `GET /api/tasks/{id}/recovery` **404s** — ruling 3 deleted it, "
        "so nothing claims it is live — and every GUI-rendered value on the detail response); unit `:367`. "
        "PARTIAL, stated: whole-diff marking, not per-hunk. `dod4_gui_typecheck.txt`: `tsc` exit 0. "
        "Full row: README DoD 4.",
        True,
    ),
    (
        "DoD 5 — offline tests simulate the timeout with a dirty worktree and prove a broken "
        "intermediate state is surfaced by verification, not accepted.",
        "In `ba77a43` + `05011a4` (the LINT half, which `ba77a43` lacked), verified by running. "
        "`tests/unit/test_recovery_markers.py` (16) + `tests/integration/test_recovery_after_timeout.py` "
        "(5), all offline on temp git repos with mock harness/provider. "
        "`::test_timeout_wreckage_is_committed_as_recovery_and_told_to_the_next_run:191-309` leaves "
        "`def handle(): return not_yet_defined(1)` — run 5's actual defect class — and asserts the gate "
        "fails on BOTH commands: `tests` fail (`test.log` names `not_yet_defined`) AND `lint` fails "
        "(`lint.log`: `app.py:2: F821 undefined name 'not_yet_defined'`); the Worker's "
        "`READY_FOR_REVIEW`+`met: true` story is contradicted and no reviewer is consulted. Measured at "
        f"this HEAD: {SUITE_PASSED}/{SUITE_FAILED}/{SUITE_SKIPPED} full suite, targeted 16/5/21/14/5/40 "
        "(`targeted_tests.txt`).",
        True,
    ),
    (
        "DoD 6 — a named terminal event for a normally completed run, additive only, documented "
        "alongside the existing vocabulary, with the T-9 trigger set expressed against it.",
        "Emitter in `ba77a43`; the docs it referenced but never wrote came in `232b6d4`, reworded in "
        "`21f79c4` + round 6. `run_service.py:70-90` — `_set_run` is the only writer of `run.status` and "
        "emits `run_completed` (`:85`) on every transition to `completed`. Per ruling 2 "
        "`run_failed`/`run_timeout` were NOT moved (they keep their run-level error context in `_execute`); "
        "the split and reasoning are at `docs/build-loop.md:333-378`, the vocabulary at `:321-330`. No event "
        "renamed, no line-shape key changed. Consumer audit: no notify/webhook code on this branch and no "
        "in-repo consumer infers run success from a task-status transition (Build Log renders events as they "
        "arrive; Dashboard counts `task.status`, a task verdict) — nothing to migrate; T-9's trigger set is "
        "written against these events at `build-loop.md:361-370`. Proof: integration `:447` "
        "`::test_normal_completion_is_a_terminal_event_and_needs_no_recovery`. Contract question filed as "
        "CCR-T8-01 (PROPOSED); `docs/frozen-contracts.md` untouched.",
        True,
    ),
    (
        "DoD 7 — a retry after `run_timeout` leaves no contradictory lifecycle fields; decided, "
        "documented, tested, and checked against the GUI header's payload.",
        "Reset in `ba77a43`; the GUI-payload assertion added in `05011a4`. Decided set = "
        "`task_service.py:137` `ROUND_FIELDS = (handoff_status, review_status, verification_status, "
        "merge_status)`; `:145` `clear_round_fields` clears only what is set and logs "
        "`retry_fields_reset`; called from `run_service.py:255` at the START of a round and deliberately "
        "NOT on a requeue (a queued task must keep showing the `CHANGES_REQUESTED` verdict the Worker acts "
        "on); `status` stays the frozen state machine's business. Documented at "
        "`docs/build-loop.md:122-136`. Proofs: unit `:407` (RUN_TIMEOUT + CHANGES_REQUESTED + fail + stale "
        "→ cleared, logged, no empty churn); integration `:191` snapshots the header WHILE the new round "
        "is live — `status=in_progress` with `handoff_status=None` on the ORM row AND on the `_detail` "
        "payload the header renders; integration `:463` (requeue keeps the verdict, new round clears it).",
        True,
    ),
    (
        "DoD 8 — unauthorized contract edits detected deterministically by the harness, with a "
        "structured build-log finding, a harness-detected reviewer-payload entry, and a decided route.",
        "In `ba77a43`; REWRITTEN by ruling 1 (`21f79c4`), split per case in round 6. "
        "`project_service.py:29-45` `CONTRACT_FILES` = the five docs `contract_files()` knows; `:140` "
        "`_touched_paths` = branch diff **+ worktree dirt** (tracked AND untracked, so an uncommitted edit "
        "cannot hide); `:107-132` `authorized_contract_files` is a **GRANT test, not a mention test** "
        "(`:53-67` `_GRANT_RE` needs a change verb; `:68-79` `_PROHIBIT_RE` vetoes any negation; sources = "
        "`definition_of_done` + `acceptance_criteria` + `frozen_contracts` ONLY, never description, commit "
        "message or diffstat); `:47-51,134-138,155-175` set ops + `contract_findings`. "
        "`supervisor_service.py:46,211-262` fails `CONTRACT_CHECK` and emits `contract_change_detected` with "
        "`findings_json` naming the recovery commits; `:353-393,414` `contract_change_hold` — DECISION: "
        "straight to `blocked` + `review_status=NEEDS_HUMAN`, no review round burned, because a requeue "
        "would ask the Worker to fix what it may not fix (`docs/build-loop.md:137-168`); `review_service.py"
        ":114-137` gives the reviewer the same computed numbers. PER CASE (each name printed with its PASS "
        "line in `ruling1_grant_tests.txt`): (a) prohibition still flags → `test_recovery_markers.py:235-276`; "
        "(b) explicit grant suppresses → `:278-322`; (c) commit message / diffstat / description grant "
        "nothing → `:324-351`; (d) bare mention → `:353-365`. DoD's required shape: integration `:365-437` — "
        "a `docs/frozen-contracts.md` edit swept INTO a recovery commit, tests green, still fails → "
        "`blocked`/`NEEDS_HUMAN`/`recovery_commit: true`; `:417-425` a mention does not release the hold, "
        "`:429-437` a grant does. This task's own `docs/frozen-contracts.md`: 0 commits, 0 dirty.",
        True,
    ),
    (
        "DoD 9 — the dirty predicate split by decision (merge/doctor on TRACKED dirt, recovery still "
        "sweeping untracked output) and tested as a split.",
        "In `ba77a43`, verified by reading. `utils/git.py:70` `status_porcelain(tracked_only=)` (`-uall`, "
        "`-uno` when tracked_only) + `:87` `status_entries` (NUL-safe XY); `git_service.py:60` "
        "`DirtyStatus`, `:212` `status_split`, `:221` `is_dirty` (inclusive, meaning unchanged), `:228` "
        "`tracked_dirty`. `merge_no_ff:475` refuses on TRACKED dirt only and names the untracked half; "
        "`project_service.doctor:279` keys 'main checkout clean' on tracked dirt and adds a separate "
        "always-ok check listing untracked paths; `recovery_commit_info:291` STILL sweeps untracked worker "
        "output and reports `tracked_modified` vs `untracked_added` separately in the commit body AND the "
        "event payload; `remove_worktree:519` keeps the inclusive predicate so nothing is discarded. "
        "Proofs (a)-(d): `test_recovery_markers.py:124` (predicates), `:285` (doctor ok=true with untracked "
        "listed separately, then ok=false on a real tracked edit), `:445` (tracked dirt still blocks the "
        "merge; untracked-only worktree still swept), `:143` (cleanup still refuses to discard untracked "
        "output), `tests/integration/test_git_worktree.py::test_merge_gate_refuses_tracked_dirt_but_not_untracked`. "
        "No control widened: subject prefix, event names, `.managed/<task_id>` and never-delete-branches stand.",
        True,
    ),
    (
        "DoD 10 — credential patterns anchored with the negative lookbehind `(?<![A-Za-z0-9])` (NOT a "
        "word boundary): names and `findings_json` payloads survive, credentials still redact.",
        "Worker-authored (`9001e82`, sharpened in `94db0cc`) — NOT recovery wreckage. "
        "`utils/redaction.py:22-23` are now `(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{10,}` and "
        "`(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}`, with the measured damage beside them. The "
        "lookbehind, not `\\b`, because `_` is a word character: `\\bsk-` would miss `TOKEN_sk-proj-…` / "
        "`API_KEY_sk-ant-…` and so would have weakened redaction while fixing the names — the operator's "
        "original 'word boundary' wording is corrected here, in `docs/build-loop.md:364-378` and in every "
        "committed note, so nothing argues for `\\b` any more. It matters because `log_service.py:28` redacts "
        "EVERY event's `details`: build_log_entries.id=86 handed the operator a merge command whose branch "
        "name was destroyed, and this task's `findings_json` payload loses a value while still parsing. "
        "Proofs: `tests/unit/test_redaction.py` (14 tests, measured at the capture HEAD) incl. "
        "`::test_branch_task_and_phase_names_survive_redaction`, `::test_findings_json_payload_survives_redaction`, "
        "`::test_credentials_are_still_redacted_wherever_they_appear`, "
        "`::test_build_log_details_keep_operational_commands_copy_pasteable`, "
        "`::test_underscore_joined_other_credentials_are_a_recorded_gap_not_a_regression`; plus "
        "`dod10_redaction_probe.txt`, which runs OLD (at `14a221c`) / BOUND (`\\b`) / SHIPPED over the same "
        "strings — the `merge_candidate` command is whole only under SHIPPED, and the underscore-glued key "
        "leaks only under BOUND. Trade stated, not hidden: a credential glued to a letter (`xsk-proj-…`) is "
        "no longer caught; no anchor can both spare `task-` and catch that.",
        True,
    ),
]

ASSUMPTIONS = [
    f"DoD 1-9's implementation content is `ba77a43`'s — a `chore(orchestrator-recovery)` commit the "
    f"harness created after run 10 was SIGTERMed with no handoff. It was verified by reading the named "
    f"file:line and by running the committed tests, not re-authored, and not amended/rebased/re-split. "
    f"Where an item was not literally satisfied by that content, the gap is closed in a separate "
    f"declared commit and named in the item's evidence (DoD 5 lint half `05011a4`; DoD 6/7 docs "
    f"`232b6d4`; DoD 7 GUI payload `05011a4`; DoD 8 grant semantics `21f79c4` + this round).",
    f"Every committed number in this handoff was produced at ONE HEAD ({HEAD}) by "
    f"`verify-evidence/T-8/suite_and_lint.sh`, `targeted_tests.sh`, `ruling1_grant_tests.sh`, "
    f"`dod1_recovery_machinery.sh`, `dod10_redaction_probe.sh` and `dod4_gui_typecheck.sh`; each .txt "
    f"carries `HEAD=<sha>` in its first line. `src/`, `tests/`, `web/` and `docs/` were clean at "
    f"capture (`git status --porcelain -- src tests scripts web docs` empty); the only dirty entries "
    f"were the evidence .txt files being rewritten by the same run. Three full-suite invocations "
    f"happened this round: one pre-capture check at `00c14ec` (421/0/6), the captured run at "
    f"`4ca2e3b` (421/0/6), and the committed run at `bd9239f` (421/0/6) — same counts at all three, "
    f"and the two intervening commits added only files under `verify-evidence/`.",
    "Ruling 2 is taken as settling the 'same chokepoint' question earlier rounds filed: `run_completed` "
    "stays in `_set_run`, `run_failed`/`run_timeout` stay in `_execute`, and the docs state the split. "
    "No emitter moved, so no log volume or ordering changed.",
    "Ruling 3 is taken as settling DoD 4's API surface: `recovery_commits` on the task detail response "
    "is the only serving surface; there is no endpoint, and the integration test asserts the 404.",
    "Ruling 4 is implemented as a CCR, not a contract edit: `docs/frozen-contracts.md` is untouched "
    "(`0` commits, `0` dirty — see `ruling1_grant_tests.txt`). CCR-T8-02's field description now "
    "matches the emitted types exactly, including `author: str` formatted `%an <%ae>`.",
    "Recovery-commit detection is trailer-only, so pre-trailer recovery commits (`ba77a43`, `5a0cd1a`) "
    "are NOT flagged by the machinery and cannot be without the amendment that was refused. Documented "
    "as the 'Boundary' in `docs/build-loop.md:114-121` and stated in this handoff instead.",
    "Ruling 1's grant vocabulary is a heuristic over English DoD text; it FAILS CLOSED — an "
    "unrecognised grant phrasing raises a finding and routes to NEEDS_HUMAN for a human. The direction "
    "of the residual risk is deliberate: a grant that should authorize and does not is a nuisance, the "
    "reverse was the bug.",
    "Review-input truncation is a HARNESS limitation, recorded not fixed here (round-5 operator ruling "
    "on finding 6; round-6 reviewer finding 7 repeats it). This round therefore changed no "
    "review/verification plumbing — in fact it REMOVED the one unauthorized artifact that round-2 "
    "wreckage had added — and mitigates by keeping this block short, pointing at committed evidence, "
    "and putting the decision-critical fields near the top and bottom rather than the middle.",
    f"The archived copy of this handoff will render the branch name mangled (`ta[REDACTED]…`) because "
    f"`artifact_service.write_text` redacts and the running harness is `main@{MAIN}`, which does not "
    f"yet carry DoD 10's lookbehind. The on-disk `.orchestrator/handoff.json` in the worktree is "
    f"byte-exact; the fix is already on this branch.",
]

QUESTIONS = [
    "CCR-T8-01 (docs/contract-change-request-T8-build-log-events.md, PROPOSED): freeze `run_completed` "
    "and/or the ` | findings_json=` marker in docs/frozen-contracts.md? This branch would pick option "
    "3 (freeze only the event name), because the marker rides inside a frozen string and is the easier "
    "part to break by accident.",
    "CCR-T8-02 (docs/contract-change-request-T8-02-recovery-commits-field.md, PROPOSED): freeze the "
    "additive `recovery_commits` field on GET /api/tasks/{id}? Safest option 1 (approve as written, "
    "zero code change); option 4 (split `author` into {name,email}) is now listed and NOT recommended, "
    "because it would break an emitted field the client already reads as a string.",
    "DoD 4's 'GUI diff marks recovery commits distinctly' is a warning block above the whole-diff view "
    "plus a header badge, not per-hunk attribution, because patch.diff is one flat diff against the "
    "base branch. Accepted as-is by rulings 3+4; per-commit hunks would be an artifact-format change "
    "beyond this phase — confirm that boundary is right.",
    "Should pre-trailer recovery commits (ba77a43, 5a0cd1a) get an explicit 'legacy recovery commit' "
    "annotation in the reviewer payload, or is the documented Boundary enough? The annotation would be "
    "a subject-based fallback, which is exactly what the trailer rule rejects.",
    "Ruling 1's grant test reads task.frozen_contracts as an authorization source (the ruling names "
    "it). A frozen-contracts entry that merely QUOTES a contract path — which is what this project's "
    "frozen list does — grants nothing. Confirm that reading before a future phase relies on it.",
    "Review-input size policy (finding 7 / the separately tracked item): should the reviewer payload "
    "get an explicit budget with a 'N of M files shown' header, or should the full payload be archived "
    "by hash — which needs a CCR for the frozen runs/<run_id>/ layout? T-8 deliberately did neither.",
]

CONTRACTS_TOUCHED = [
    "docs/frozen-contracts.md: NOT edited. The two contract questions are filed as CCR-T8-01 "
    "(docs/contract-change-request-T8-build-log-events.md) and CCR-T8-02 "
    "(docs/contract-change-request-T8-02-recovery-commits-field.md), both status PROPOSED.",
    "Build-log line shape {timestamp, phase, task_id, run_id, role, event, details, commit_sha}: "
    "unchanged. The structured recovery payload rides INSIDE the existing `details` string.",
    "Existing build-log event names: unchanged, additive only. `run_completed` is the added terminal "
    "success name; `run_failed`/`run_timeout` stay emitted from `_execute` (ruling 2), split documented "
    "in docs/build-loop.md.",
    "`chore(orchestrator-recovery):` commit-subject prefix and the `recovery_commit` event name: "
    "preserved byte-exact, trailer added alongside.",
    "Frozen artifact directory layout runs/<run_id>/: UNCHANGED as of round 6 — the `review_payload.md` "
    "file that round-2 wreckage had added is removed, so this branch adds no artifact name to the "
    "layout.",
    "REST API shapes under /api: ONE additive field (`recovery_commits` on GET /api/tasks/{id}), "
    "declared in CCR-T8-02 (PROPOSED). No new route: the one added in round 2 was deleted in round 5 "
    "(ruling 3).",
    "Task/run state machine core/state.py: not modified. Merge-gate semantics: unchanged — only the "
    "dirtiness predicate it keys on was narrowed to tracked content (DoD 9), which is what makes the "
    "refusal and the doctor verdict correct for untracked-only checkouts.",
    "Cleanup contract: still only worktrees under worktree_root carrying .managed/<task_id>, and "
    "branches are never deleted. `remove_worktree` keeps the INCLUSIVE dirty predicate so no worker "
    "output is ever discarded.",
    "Contract-file authorization semantics (project_service.authorized_contract_files): narrowed from "
    "'mentioned' to 'explicitly granted' — a security control tightened, never loosened (ruling 1).",
]

MEMORY = [
    (
        "project",
        "gotchas",
        "coding-orchestrator: authorization checks over natural-language DoD text must be GRANT tests, "
        "not mention tests. project_service.authorized_contract_files used to match a contract path "
        "anywhere in definition_of_done/acceptance_criteria, which meant T-8's own DoD 8 — the item "
        "that lists all five contract files to describe the detector — and even 'Do NOT modify "
        "docs/frozen-contracts.md' both counted as authorization, so the contract-edit control failed "
        "open and was blind in the common case. Since T-8 round 5 a clause authorizes only if it names "
        "the file AND grants a change verb, any negation in the clause vetoes it, and the only sources "
        "are definition_of_done + acceptance_criteria + frozen_contracts (never description, commit "
        "message or diffstat). It fails closed: an unrecognized grant phrasing routes to NEEDS_HUMAN.",
        ["contract-guardrails", "redaction-vs-authorization", "fail-closed"],
    ),
    (
        "project",
        "gotchas",
        "In coding-orchestrator, utils/redaction.py's sk- credential patterns were unanchored until "
        "T-8: `sk-(?:proj-)?[A-Za-z0-9_-]{20,}` matched INSIDE words ending in 'sk', and because "
        "log_service.log_event() redacts every event's `details`, build_log_entries.id=86 printed 'git "
        "merge --no-ff ta[REDACTED]' — the harness handed the operator a merge command whose branch name "
        "was destroyed, and it mangles the `branch` field of every archived handoff.json too. Fix shape "
        "actually shipped: a NEGATIVE LOOKBEHIND `(?<![A-Za-z0-9])sk-...`, NOT `\\b` — `_` is a word "
        "character, so `\\bsk-` would miss `TOKEN_sk-proj-...` and weaken credential redaction while "
        "fixing names. Any new pattern added to SECURITY_PATTERNS must carry the same anchor.",
        ["redaction", "build-log", "branch-names"],
    ),
    (
        "project",
        "conventions",
        "coding-orchestrator now has two dirty predicates and the split is deliberate: "
        "git_service.tracked_dirty() (`git status --porcelain -uall -uno`) answers 'did versioned "
        "content change?' and gates merge_no_ff's refusal and project doctor's 'main checkout clean' "
        "check; git_service.is_dirty() (untracked included) answers 'is anything uncommitted?' and is "
        "what recovery_commit and remove_worktree use, because untracked files are worker output and "
        "must never be discarded. Recovery commits report the halves separately "
        "(DirtyStatus.tracked_modified / untracked_added) in the commit body and in the recovery_commit "
        "event's findings_json payload. -uall is used, not the default, so a new directory lists files "
        "and the paths agree with `git diff --cached --name-status` after staging.",
        ["git", "worktrees", "recovery-commits"],
    ),
    (
        "project",
        "decisions",
        "T-8 decisions recorded in docs/build-loop.md: (1) a recovery commit is machine-identifiable by "
        "the git trailer `Orchestrator-Recovery: <run_short_id>` and NEVER by subject string matching "
        "(a hand-authored commit can copy the subject), so pre-trailer recovery commits in history stay "
        "unflagged — documented as the Boundary; (2) every terminal run status has exactly one named "
        "event — run_completed is the newly named success event, emitted from run_service._set_run, the "
        "only writer of run.status, while run_failed/run_timeout stay in _execute with their error "
        "context; notification fan-out must key off events, not infer success from a task_status "
        "transition; (3) an unauthorized contract-file hit is computed by the harness (branch diff + "
        "worktree dirt, intersected with project_service.contract_files, authorized only by an EXPLICIT "
        "GRANT in the stored definition_of_done / acceptance_criteria / frozen_contracts — a mention or "
        "a prohibition names nothing) and routes straight to blocked with review_status=NEEDS_HUMAN "
        "instead of burning a review round; (4) handoff_status/review_status/verification_status/"
        "merge_status are per-round fields cleared at the START of a round (task_service."
        "clear_round_fields), never on a requeue, so a queued task still shows the CHANGES_REQUESTED "
        "verdict the Worker must act on; (5) runs/<run_id>/ is a frozen layout — do not add artifacts "
        "like review_payload.md to it without a contract-change request.",
        ["recovery-commits", "build-log", "contract-guardrails", "lifecycle"],
    ),
    (
        "role",
        "worker",
        "On this harness a Worker round that times out can still land a complete, lint-clean, "
        "test-green implementation — as UNDECLARED content inside a chore(orchestrator-recovery) commit "
        "(measured: ba77a43, 16 files +1627/-46, independently measured 411 passed/6 skipped). The "
        "correct next-round protocol is: write handoff.json + handoff.md FIRST, verify each DoD by "
        "reading the named file:line, fix only what is actually missing, and state per item plainly "
        "that the content arrived undeclared in a recovery commit. Never amend, rebase or re-split the "
        "recovery commit into neat feature commits — laundering wreckage into apparent intentional "
        "work is the defect. Round 6 addendum: a reviewer WILL find defects inside that wreckage (it "
        "added a file to the frozen runs/<run_id>/ layout with no DoD grant), so the honest move is to "
        "name each undeclared addition in the handoff before being asked.",
        ["timeout", "recovery-commit", "handoff-discipline"],
    ),
    (
        "global",
        "environment",
        "To type-check a git worktree of Coding-Orchestration-Agent-Harness that has no "
        "web/node_modules: build a scratch dir under /tmp, symlink <main>/web/node_modules into it and "
        "symlink the worktree's web/{src,index.html,package.json,tsconfig.json,vite.config.ts,tailwind."
        "config.js,postcss.config.js} in, then run ./node_modules/.bin/tsc -b --force (exit 0 = clean) — "
        "and prove which sources were read with `tsc -p tsconfig.json --noEmit --listFiles`. Do NOT put "
        "the node_modules symlink inside the worktree: .gitignore's `web/node_modules/` has a trailing "
        "slash so it matches directories only, and a symlink would show up as untracked dirt that "
        "recovery_commit would then sweep onto the branch under review.",
        ["git-worktree", "typescript", "web", "verification"],
    ),
    (
        "project",
        "conventions",
        "Evidence for a gated run in coding-orchestrator must be committed under a real, "
        "reviewer-reachable path: `verify-evidence/<task>/` with `.txt` outputs and a README that maps "
        "each DoD item to file:line and to the log that proves it. `.orchestrator/` is gitignored and "
        "nothing under it is archived into runs/<run_id>/, and `*.log` is gitignored too. Since T-8 the "
        "capture scripts stamp the HEAD they ran at into each file (suite_and_lint.sh writes "
        "`# HEAD=<sha> … tree=<n> dirty-entries`), because two rounds in a row quoted counts taken from "
        "different HEADs while claiming one run. Note for pytest here: pyproject sets addopts=\"-q\", so "
        "a single -v cancels it — use -vv when evidence must name individual tests.",
        ["evidence", "verify-evidence", "pytest", "handoff"],
    ),
]

handoff = {
    "phase": "T-8",
    "branch": git("branch", "--show-current"),
    "status": "CHANGES_APPLIED",
    "summary": SUMMARY,
    "files_changed": FILES,
    "tests": {
        "command": "./scripts/orch_verify.sh test",
        "passed": SUITE_PASSED,
        "failed": SUITE_FAILED,
        "skipped": SUITE_SKIPPED,
        "log_path": "verify-evidence/T-8/full_suite.txt",
    },
    "dod_self_check": [
        {"item": item, "met": met, "evidence": ev} for item, ev, met in DOD
    ],
    "new_dependencies": [],
    "assumptions": ASSUMPTIONS,
    "questions": QUESTIONS,
    "blockers": [],
    "contracts_touched": CONTRACTS_TOUCHED,
    "memory_suggestions": [
        {"category": c, "scope": s, "content": body, "tags": tags}
        for c, s, body, tags in MEMORY
    ],
}

Handoff.model_validate(handoff)  # fail loudly if the frozen schema is violated
json.dump(handoff, open(".orchestrator/handoff.json", "w"), indent=2, ensure_ascii=False)
print(
    f"wrote .orchestrator/handoff.json  HEAD={HEAD} main={MAIN} behind={BEHIND} ahead={AHEAD} "
    f"suite={SUITE_PASSED}/{SUITE_FAILED}/{SUITE_SKIPPED} files={len(FILES)} "
    f"dod={len(DOD)} deps=0 blockers=0 questions={len(QUESTIONS)} memory={len(MEMORY)}"
)
