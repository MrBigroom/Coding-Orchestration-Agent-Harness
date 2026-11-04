# Contract-change request CCR-T8-01 — name the terminal success event and the structured `details` payload

- **Status:** APPROVED **as corrected** by the operator, 2026-11-04. Applied to
  `docs/frozen-contracts.md` ("Build-log events") with the original overclaim fixed: `cancelled` is a
  fourth terminal status that still has no named event, so `run_cancelled` is recorded as a T-9
  requirement rather than frozen as if it existed. This file is kept as the request of record.
  `docs/frozen-contracts.md` is untouched on this branch.
- **Raised by:** T-8 (`task-8-recovery-commits-after-a-timeout-commit-half-fin`), 2026-11-04.
- **Why this is a request and not a change:** T-8's Definition of Done authorizes *adding* a
  terminal event name and a structured field, and it explicitly says the build-log **line shape**
  `{timestamp, phase, task_id, run_id, role, event, details, commit_sha}` and the **existing event
  names** stay frozen. It does not authorize editing `docs/frozen-contracts.md`. So the two
  conventions below are documented in `docs/build-loop.md` only, and this file records that the
  option of freezing them is a human decision — the same shape T-6 used
  (`contract-change-request-T6-orch-verify-marker.md`).

## What the code already depends on

**1. `run_completed` — the terminal success event.** `run_service._set_run()` (the only writer of
`run.status`) emits event `run_completed` whenever a run reaches `completed`, with details
`"<run_short_id> finished normally (exit <code>)"` and `role="worker"`. The failure counterparts
`run_failed` / `run_timeout` are named in `docs/build-loop.md`; before T-8 there was **no** event
for a run that finished normally, so a consumer could only infer success from a `task_status`
transition. T-8's DoD 6 (the terminal success event itself) needs the event to be dependable, and
the T-9 notification fan-out is
specified against it (`docs/build-loop.md`, "Terminal run events").

**2. `| findings_json=` — a structured payload inside the frozen `details` string.**
`utils/structured_log.py` appends ` | findings_json={<json object}>` as the **last** segment of
`details`, so the line shape does not change and old consumers keep showing the human summary.
Producers: `git_service.recovery_event_details` (the `recovery_commit` event, DoD 1) and
`supervisor_service` (the `contract_change_detected` event, DoD 8). Consumers:
`git_service.parse_recovery_event_details`, `structured_log.parse`, and the Build Log GUI. A line
with no marker parses to `None`, so legacy lines stay readable.

Both conventions are already pinned by tests:
`tests/integration/test_recovery_after_timeout.py::test_normal_completion_is_a_terminal_event_and_needs_no_recovery`,
`::test_timeout_wreckage_is_committed_as_recovery_and_told_to_the_next_run`,
`::test_unauthorized_contract_edit_from_recovery_waits_for_a_human`, and
`tests/unit/test_recovery_markers.py::test_recovery_event_details_are_machine_readable`,
`::test_structured_log_roundtrip_and_absence`.

## Proposed contract text (for the Architect to place in `frozen-contracts.md` if approved)

> **Build-log events**: the line shape `{timestamp, phase, task_id, run_id, role, event, details,
> commit_sha}` is frozen. A run's terminal status is one of `completed | failed | timeout |
> cancelled`, and **three of those four** have a named terminal event: `run_completed` (normal
> completion), `run_failed`, `run_timeout`. A `cancelled` run has **no** named event today — it is
> observable only as a `task_status` line reading `run cancelled` — and `run_cancelled` is NOT an
> event name yet; adding it is a T-9 requirement, deliberately not frozen here as if it existed.
> `run_error` is not a terminal status: it labels an unexpected exception inside the harness, after
> which the run still ends as `failed`. Event names are additive — a phase may add one, never rename
> or remove one. Where a
> machine consumer needs structure, `details` may carry a final segment
> ` | findings_json=<json object>`; the segment is always last, `json.loads` of the remainder is
> the payload, its absence means no payload, and no payload may contradict the human summary in
> the same string.

## Safest options

1. **Approve as written.** Zero code change — the names and the marker are already what the code
   emits and parses. Cost: the frozen list of event names becomes a thing future phases must
   extend deliberately rather than freely.
2. **Leave both unfrozen** (this branch's state). Documented in `docs/build-loop.md` only.
   Cost: a later phase could rename `run_completed` or reuse the `findings_json=` marker with a
   different meaning without tripping any contract check — the exact ambiguity T-8 was created to
   remove.
3. **Freeze only `run_completed`** and leave the `findings_json=` marker as a repo-internal
   convention. Middle ground: the notification fan-out (T-9) gets a stable trigger while the
   payload encoding stays free to evolve. **This is the option this branch would prefer if only
   one change is approved**, because the marker is carried inside a frozen string and is therefore
   the part a reader could most easily break by accident.

## Blocking?

No. T-8 completes under option 2 — DoD 1, 6 and 9 are satisfied with the line shape and the
existing event names unchanged, and `docs/frozen-contracts.md` was not edited. This artifact
exists so the decision is recorded rather than silently made by the Worker.
