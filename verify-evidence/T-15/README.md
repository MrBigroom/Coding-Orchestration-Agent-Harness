# T-15 evidence index

Regenerate this directory with one command, from the worktree at the HEAD you want to certify:

```bash
orchestrator task evidence T-15            # or: orchestrator task evidence 15 --root .
```

The command runs `capture.sh` (committed here), rewrites every stamp it finds to the canonical
`# HEAD=<full sha>` form, and writes `manifest.json` with the HEAD, timestamp, command line, tool
versions and produced files. It needs no network or API key and reports how many files it rewrote.

## Files

| file | what it proves |
|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` at the stamped HEAD |
| `lint.txt` | `./scripts/orch_verify.sh lint` at the stamped HEAD |
| `freshness_tests.txt` | the focused `-vv` run naming the T-15 unit/integration tests and the split memory-scope tests |
| `dod_map.txt` | DoD 5(a)-(f) mapped to the test node ids whose `PASSED` lines appear in `freshness_tests.txt` |
| `source_anchors.txt` | `grep -n` anchors for the check, parser, generator, Supervisor/review wiring and CLI command |
| `freshness_selfcheck.txt` | `check_freshness` run against the committed bundle at the evidence commit: `status=pass` |
| `manifest.json` | generated manifest (HEAD, generated_at, command, tool versions, file hashes) |
| `capture.sh` | the committed generator; no transcript-only evidence |

## DoD item -> implementation -> test -> evidence

| DoD | implementation | regression test | evidence |
|---|---|---|---|
| 1. deterministic freshness check, harness-detected, own check name/status, no verdict flip | `src/orchestrator/services/evidence_freshness.py` (`CHECK_NAME`, `parse_stamps`, `check_freshness`); `supervisor_service.py` adds the row and `evidence_freshness_stale` event; `review_service.py` adds the harness finding | `tests/unit/test_evidence_freshness.py::test_an_older_stamp_is_stale_with_the_paths_and_the_count`; `tests/integration/test_evidence_freshness.py::test_stale_evidence_is_reported_as_stale_and_does_not_flip_the_verdict`; `::test_strict_mode_is_the_only_way_staleness_fails_the_gate` | `freshness_tests.txt` PASSED lines; `source_anchors.txt`; `freshness_selfcheck.txt` |
| 2. one generator, one manifest (HEAD, timestamp, command, tool versions, files) | `regenerate_bundle`; CLI `orchestrator task evidence`; committed `verify-evidence/T-15/capture.sh` | `tests/unit/test_evidence_freshness.py::test_the_generator_writes_the_manifest_fields_and_one_canonical_stamp`; `::test_a_missing_generator_is_an_error_not_a_silent_stamp_refresh` | `manifest.json`; `freshness_tests.txt` |
| 3. cheap, offline, idempotent; unchanged files left alone; reports rewrites | `regenerate_bundle` compares content hashes and preserves a no-op timestamp | `tests/unit/test_evidence_freshness.py::test_regeneration_is_one_command_and_second_run_is_a_no_op` | `freshness_tests.txt`; `manifest.json` |
| 4. split conjoined assertions; exact-value reason | `tests/unit/test_memory_scopes.py` (26 splits + exact rejected reason), `tests/integration/test_memory_scope_boundary.py` (15 splits) | `test_fix_scopes_leaves_rejected_rows_alone`; `test_the_rejected_reason_assertion_fails_when_the_reason_changes`; all split tests in the focused run | `freshness_tests.txt` PASSED lines |
| 5. offline cases (a)-(f) | — | the node ids listed in `dod_map.txt` | `dod_map.txt` + `freshness_tests.txt` |
| 6. docs and convention | `docs/build-loop.md` "Evidence freshness"; `docs/decisions.md` 2026-11-08 bullet; `docs/memory.md` companion convention; README Known limitations | — (documentation) | `source_anchors.txt`; handoff |

## Honest limitation

The check reports staleness, it does not force a re-run. `stale` does not fail the gate unless
`ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true`, and a tree with no committed `capture.sh` cannot be
refreshed by the generic command (it fails with a clear message rather than silently re-stamping
old content).