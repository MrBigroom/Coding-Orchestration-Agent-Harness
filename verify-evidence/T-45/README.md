# T-45 verification evidence

Generated with `orchestrator task evidence T-45` from a clean worktree. That driver runs the
committed `capture.sh`, canonicalises the stamp on every file the pass rewrote to
`# HEAD=<sha> mode=<fast|full>`, refuses any uncommitted input, and writes `manifest.json` plus
`freshness_selfcheck.txt` itself. The stamped HEAD is the code commit measured; the evidence-only
commit that ships this bundle does not change code, so it does not stale the bundle.

| file | proves |
| --- | --- |
| `full_suite.txt` | `./scripts/orch_verify.sh test` -> full-suite counts; `ORCH_VERIFY` shows the imports resolved inside this worktree |
| `lint.txt` | `./scripts/orch_verify.sh lint` -> `All checks passed!` |
| `t45_tests.txt` | the T-45 tests named `PASSED` with `-vv`, plus the sync suites whose literal assertions were replaced |
| `command_audit.txt` | every recovery string driven out of the code path and every operator command hint found in `src/orchestrator`, each parsed by the real Typer/Click tree; the shipped `--dry-run` hint is shown being rejected by the same validator |
| `live_cli.txt` | the real `orchestrator` entry point: `sync reinit --dry-run --help` exits 2 with `No such option`, `sync reinit --help` and `sync reinit --apply --help` exit 0; `git grep` guards show no stale hint outside archival evidence |
| `tsc_web.txt` | `tsc -b --force` over this worktree's `web/src` (scratch dir + symlinked node_modules, disclosed in the file); `Settings.tsx` is in `--listFiles` |
| `source_anchors.txt` | committed-HEAD file:line anchors for the claims below |

## DoD -> evidence

1. **The hint is a command that runs, everywhere it is emitted.** `VERSION_REJECTION_RECOVERY`
   (`sync_transport.py:75-81`) is `run \`orchestrator sync reinit\` (the dry run is the default and
   \`--apply\` writes). Unresolved payload conflicts are reported as conflicts_skipped and left
   untouched`; the classifier uses it at `:149`. Corrected forms: `notification_vocab.py:57-59`,
   `static_fallback/index.html:214`, `Settings.tsx:204-207`, `docs/cloud-sync.md:114-116`,
   `docs/decisions.md:532-534`, `README.md:266-267`. `live_cli.txt` shows the old flag exiting 2 and
   the corrected command (default dry run and `--apply`) accepted; the over-promise guard is the
   `conflicts_skipped` / `left untouched` clause.
2. **The literal assertions are replaced by CLI validation.** `tests/unit/test_operator_command_hints.py`
   drives the classifier for every response class, iterates every `RECOVERY_BY_KIND` key, AST-scans
   the module so a new literal must be reachable, extracts the backticked commands and parses each
   with `make_context` (no callback runs). `test_the_validator_rejects_the_flag_that_shipped` proves
   the validator fails on the old string. `command_audit.txt` reproduces the enumeration outside
   pytest.
3. **Other operator-command strings audited.** `test_every_operator_command_hint_in_the_source_tree_parses_against_the_cli`
   scans all of `src/orchestrator` for backticked and `# command:` hints (15 today) and parses each;
   `test_the_guis_name_commands_the_cli_accepts_and_drop_the_bad_flag` does the same for the
   `<code>` templates in `static_fallback/index.html` and the `className="mono"` command in
   `Settings.tsx`. Placeholder rule: `<id>`/`{label}` holes become `T-45` before parsing. Covered
   emitters include `sync_transport` recovery strings, `notification_vocab.sync_failed`,
   `evidence_freshness` review/self-check commands (`orchestrator task evidence <id>` and
   `--at-tip`), `cli.py` memory fix-scopes/triage hints, `desktop` (`serve`, `desktop install`,
   `desktop stop`), `bootstrap`, `demo_service`, `cleanup_service`, `project_service` and the GUI
   templates. Not machine-tested and stated as such: prose commands in `docs/` and `README.md`
   (corrected by hand, guarded by `git grep` in `live_cli.txt`), and the historical captures under
   `verify-evidence/T-41/`, which still quote the old string verbatim and are deliberately not
   rewritten (they record what that round measured; the guard excludes `verify-evidence`).
   `--dry-run` mentions elsewhere are real flags: `cleanup` (`cli.py:312`) and
   `memory fix-scopes` (`cli.py:1256`).
4. **No assertion was weakened.** The old equality assertions became
   `== VERSION_REJECTION_RECOVERY` plus a substring check in `tests/integration/test_sync_api.py:112-113`
   and `tests/unit/test_sync_resilience.py:124-125`; the CLI-validation test is additive.
