# Contract-change request CCR-T15-01 — an additive `stale` status on verification check records

Status: **requested, not taken.** T-15 does not rely on this change; its code reports staleness
inside the frozen vocabulary described at the bottom of this file.

## What changes

`verification.json` (and the identical `VerificationCheck` model) would gain one additive value on
the existing `status` Literal: `pass | fail | skipped | not_applicable | stale`. No key, check name,
artifact name or response shape changes, and no existing value changes meaning. A first-class
`stale` would let a machine consumer distinguish "the bundle was compared and is out of date" from
`skipped` ("compared but not certified", the value T-15 uses today) without parsing prose.

## Why the phase did not take it

T-15's Definition of Done asks for a check with status `pass / stale / not_applicable`, but does not
authorize editing `docs/frozen-contracts.md`, and the task's own frozen line states that "the
verification.json check-record shape and the existing check names are frozen; new checks are
additive entries". `supervisor_service.contract_findings` would flag an unauthorized edit to the
contract file. So the additive status waits for operator approval, and until then:

- fresh bundle -> `pass`
- absent or unstamped evidence -> `not_applicable`
- stale bundle -> `skipped`, with `stale:` leading the `evidence` string, the paths in `notes`, and
  `state: "stale"` in the `evidence_freshness_stale` build-log payload
- `ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true` -> `fail`

## Consumers to update if approved

`web/src/components/ui.tsx` (status -> tone map), `artifact_service.render_verification_md` (renders
whatever the status is), and any operator query filtering `verification_status`.
