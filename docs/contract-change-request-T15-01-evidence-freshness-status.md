# Contract-change request CCR-T15-01 — an additive `stale` status on verification check records

- **Status:** APPROVED by the operator, 2026-11-08, and applied to `docs/frozen-contracts.md` in the
  same round (the additive fifth value plus a line naming this request and its approval date). The
  file name is `contract-change-request-T15-01-evidence-freshness-status.md`, settled by use in the
  operator ruling of 2026-11-08 (b): the earlier review objection was to the status marker, not the
  name. House style follows CCR-T9-01 and CCR-T11-01.
- **Why a request:** `verification.json` / `VerificationCheck.status` is a frozen Literal
  (`pass | fail | skipped | not_applicable`, `docs/frozen-contracts.md`), and T-15's DoD 1 asks for a
  check whose status is `pass / stale / not_applicable`. A fifth value is a contract change, so it
  was requested rather than improvised. Until approval the code carried staleness as `skipped` with
  a `stale:` prefix in the row's `evidence` string, `state: "stale"` in the
  `evidence_freshness_stale` build-log payload, and `fail` only behind
  `ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true`.

## What changes

`verification.json` (and the identical `VerificationCheck` model) gains one additive value on the
existing `status` Literal: `pass | fail | skipped | not_applicable | stale`. No key, check name,
artifact name or response shape changes, and no existing value changes meaning. `stale` means "the
bundle was compared with the commit under verification and is out of date", which is distinct from
`skipped` ("not attempted / not certified") and from `fail` (reserved for the operator's opt-in
strict rule). A run written before this change simply never uses the new value.

## What was implemented on approval

1. `core/schemas.py`: the Literal gains `stale`.
2. `services/evidence_freshness.py::check_status`: a compared-and-out-of-date bundle returns
   `stale`; strict mode still returns `fail`; absent/unstamped stays `not_applicable`.
3. `services/supervisor_service.py::verify`: the freshness row reports `stale`, with the offending
   paths in `notes` and the same `evidence_freshness_stale` build-log payload (`state: "stale"`).
4. `web/src/components/ui.tsx`: `stale -> warn` in the status-to-tone map.
5. `services/artifact_service.py::render_verification_md`: renders the row from `c.status`
   unchanged, so it emits `stale` with no code change; pinned by
   `tests/unit/test_evidence_freshness.py::test_the_stale_row_renders_into_verification_md`.
6. `docs/frozen-contracts.md`: the verification.json entry lists the fifth value and this request.

`grep -rn "not_applicable\|verification_status"` over `src/`, `web/` and `tests/` is quoted in the
handoff to show the consumer list is complete.

The check still never flips the final verdict by itself: `stale` is a reported status, `fail` is
only the operator's configurable strict rule, and a stale bundle never sets `contradicts_worker`.