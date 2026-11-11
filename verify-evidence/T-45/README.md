# T-45 verification evidence

Generated with `orchestrator task evidence T-45` from a clean worktree. That driver runs the
committed `capture.sh`, canonicalises the stamp on every file the pass rewrote to
`# HEAD=<sha> mode=<fast|full>`, refuses any uncommitted input, and writes `manifest.json` plus
`freshness_selfcheck.txt` itself. The stamped HEAD is the code commit measured; the evidence-only
commit that ships this bundle does not change code, so it does not stale the bundle.
`freshness_at_tip.txt` is regenerated with `--at-tip` after every other commit, and its own
`delivered_tip=` line is quoted (not re-stated) in the handoff.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> `937 passed, 10 skipped`; `ORCH_VERIFY` shows the imports resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `t45_tests.txt` | the 8 T-45 tests named `PASSED` with `-vv`, plus the sync suites whose literal assertions were replaced |
| `command_audit.txt` | 6 recovery strings driven out of the code path, 12 command-shaped strings found by AST in `src/orchestrator`, 5 in the two GUIs; every command parsed by the real Click tree and every flag resolved against the command that owns it; the shipped `--dry-run` **and** a bogus flag written in its own backticks are both rejected by the same validator |
| `live_cli.txt` | the real `orchestrator` entry point: `sync reinit --dry-run --help` exits 2 with `No such option`, `sync reinit --help` and `sync reinit --apply --help` exit 0; `git grep` guards show no stale hint outside archival evidence, and every remaining `--dry-run` mention |
| `tsc_web.txt` | `tsc -b --force` over this worktree's `web/src` (scratch dir + symlinked node_modules, disclosed in the file); `Settings.tsx` is in `--listFiles`; `tsc_exit=0` |
| `source_anchors.txt` | committed-HEAD file:line anchors for the claims below |

## DoD -> evidence

1. **The hint is a command that runs, everywhere it is emitted, and it says what reinit will not
   do.** `VERSION_REJECTION_RECOVERY` (`src/orchestrator/services/sync_transport.py:75-78`, used at
   `:149`) is `run \`orchestrator sync reinit\` (the dry run is the default and \`--apply\` writes).
   Unresolved payload conflicts are reported as conflicts_skipped and left untouched`.
   Corrected forms: `src/orchestrator/core/notification_vocab.py:55-59`,
   `src/orchestrator/api/static_fallback/index.html:214`, `web/src/pages/Settings.tsx:204-206`,
   `docs/cloud-sync.md:114-116`, `docs/decisions.md:532-533`, `README.md:266-268`.
   `live_cli.txt` shows the old flag exiting 2 and the corrected command (default dry run and
   `--apply`) accepted. The over-promise guard is the `conflicts_skipped` / `left untouched` clause
   in both the transport hint and the `sync_failed` notification body
   (`notification_vocab.py:57-59`), asserted by
   `test_sync_failed_notification_names_a_runnable_command_and_the_skip_behaviour`.
2. **The literal assertions are replaced by CLI validation enumerated from the code.**
   `tests/unit/test_operator_command_hints.py` drives the classifier for every response class and
   every `RECOVERY_BY_KIND` key, and an AST pass over `sync_transport` requires every
   `orchestrator ` literal to be reachable from that emitted set. The source audit walks every
   `src/orchestrator/**/*.py` string expression (`Constant`, `JoinedStr`, concatenation `BinOp`),
   resolves each command against `get_command(app)`, and matches every `--flag` in the same string
   after the command to `command.params`. `test_a_flag_in_its_own_backticks_breaks_the_audit...`
   proves a separately formatted bogus flag fails; `test_the_validator_rejects_the_flag_that_shipped`
   proves the old hint fails; `test_the_recovery_enumeration_catches_a_broken_hint_runtime_patched`
   mutates the emitter and requires a red.
3. **Every other operator-command string audited.** `command_audit.txt`: 12 command-shaped strings
   in `src/orchestrator` (from `sync_transport`, `notification_vocab`, `evidence_freshness`,
   `cli.py`, `desktop/__init__.py`, `bootstrap.py`, `demo_service.py`, `cleanup_service.py`,
   `project_service.py`) and 5 in the GUIs, including the corrected fallback `syncHint` prose at
   `index.html:214` and the flags in their own JSX spans at `Settings.tsx:205`. The scan is driven
   from the code, not a hard-coded list; it found `--at-tip`, `--full`, `--only` on
   `orchestrator task evidence`, `--apply` on `sync reinit`, `--args` on `adapters set`, etc.
   Not machine-validated, and listed in the test docstring: Markdown prose in `README.md`,
   `docs/cloud-sync.md`, `docs/decisions.md` (hand-corrected, guarded by `git grep`), bare
   subcommand/flag fragments that name no command in the same string (for example
   `docs/memory.md:106`, `docs/product-spec.md:1169`, and the docstring at
   `src/orchestrator/services/git_service.py:394`; all use real `--dry-run` flags declared at
   `cli.py:312` and `cli.py:1256`), and historical evidence under `verify-evidence/`. Files/paths
   scanned to establish that: `src/orchestrator/**/*.py` (115 files),
   `web/src/**/*.tsx`/`*.ts` (29 files), `src/orchestrator/api/static_fallback/index.html`.
4. **No assertion was weakened.** The old equality assertions remain
   (`tests/integration/test_sync_api.py:112`, `tests/unit/test_sync_resilience.py:124`), now paired
   with substring checks; the CLI-validation tests are additive. The corrected string is still
   asserted (`test_version_rejection_recovery...`, `--dry-run` absent and
   `conflicts_skipped`/`left untouched` present), it just stops being the only check.