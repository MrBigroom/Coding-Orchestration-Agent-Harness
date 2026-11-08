# T-15 evidence index

Regenerate this directory with one command, from the worktree at the HEAD you want to certify:

```bash
orchestrator task evidence T-15            # runs capture.sh, writes manifest.json + self-check
orchestrator task evidence T-15 --check    # read-only: prints the state, exits 1 when stale
```

`capture.sh` is the committed generator (fast pass by default; `T15_FULL=1` adds the ~2-minute
suite). The command refuses a worktree with uncommitted tracked changes — a stamp would name a
HEAD whose tree was never measured — canonicalises only the files the pass rewrote, and writes
`manifest.json` with the HEAD, timestamp, command line, tool versions and produced files. There is
no stamp-only mode: a file the generator did not rewrite keeps its old stamp and is reported stale
by the Supervisor check. Measured wall time and the rewrite count are printed by that command and
recorded in `timing.txt` (full pass) and `zz_freshness_at_tip.txt`.

| file | what it proves |
|---|---|
| `full_suite.txt` | whole suite at the stamped HEAD (written by the `T15_FULL=1` pass only) |
| `lint.txt` | `./scripts/orch_verify.sh lint` at the stamped HEAD |
| `freshness_tests.txt` | the `-v` run naming every T-15 test plus the two DoD-4 memory nodes |
| `dod_map.txt` | DoD 5(a)-(f) mapped to the node ids whose `PASSED` lines are in `freshness_tests.txt` |
| `source_anchors.txt` | `grep -n` anchors for the check, parser, status mapping, generator and wiring |
| `timing.txt` | per-step milliseconds of the pass (evidence for DoD 3, not a gate) |
| `freshness_selfcheck.txt` | the generator's own worktree-basis check of this directory |
| `zz_freshness_at_tip.txt` | the committed-view `--check` run at the delivered tip (no HEAD stamp) |
| `manifest.json` | generated manifest: HEAD, generated_at, command, tool versions, file hashes |

DoD 4 assertion inventory: at the base commit `tests/unit/test_memory_scopes.py` had 26 and
`tests/integration/test_memory_scope_boundary.py` 15 `assert a and b` statements (AST). The brief's
"28 / 16" does not reproduce on any commit in either file's history (the maximum is 27 / 18), so the
baseline is the measured 26 / 15. Sites that could fail in two unrelated ways are split; the
remainder (14 / 8) are one outcome over two fields and are documented at the top of each file, and
`tests/unit/test_evidence_freshness.py::test_the_memory_scope_tests_keep_their_assertion_shape` pins
both counts. Every `fix_scopes` reason is compared by exact value, and
`test_the_rejected_reason_assertion_fails_when_the_reason_changes` proves the assertion goes red
when the reason text changes.

## Honest limitations

The check reports staleness; it does not force a re-run. Staleness is `skipped` with `stale:` in
the row's evidence string rather than a new status value, because the verification status vocabulary
is frozen (`docs/contract-change-request-T15-01-evidence-freshness-status.md`). A bundle with no
committed `capture.sh` cannot be refreshed by the generic command, and the full-suite file is
refreshed only by the opt-in full pass.