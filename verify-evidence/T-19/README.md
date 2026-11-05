# T-19 evidence — gated runs execute in a different environment

Source under review: `1e2e522c84d571552b616e4b27e20182472dfdcf`. Evidence files are stamped with
that HEAD; this `verify-evidence/T-19/` commit changes no source or test file.

| File | Proof |
|---|---|
| `full_suite.txt` | `./scripts/orch_verify.sh test` → `615 passed, 6 skipped`, exit 0. |
| `lint.txt` | `./scripts/orch_verify.sh lint` → `All checks passed!`, exit 0. |
| `full_suite_parity.txt` | Named DoD 2 test PASSED: full suite twice in a throwaway worktree (clean, then seeded `.env`), identical counts asserted. |
| `t19_tests.txt` | Named (`-vv`) run of the new tests: `14 passed` (parity test deselected; captured above). |
| `coverage_mutation.txt` | Removing the Settings derivation reports the T-9 keys as coverage gaps and prints `MUTATION_DETECTED`. |
| `capture.sh` | Re-runs the captures at a clean code HEAD. |

## DoD mapping

1. **Derived SECRET_ENV.** `src/orchestrator/core/config.py:154` derives every
   `<ORCHESTRATOR_><FIELD>` from `Settings.model_fields`; `tests/conftest.py:22` builds
   `SECRET_ENV = (*PROVIDER_ENV_NAMES, *settings_env_names())` (provider SDK keys remain explicit
   because they are not Settings fields). Tests: `tests/unit/test_env_guard.py:31` (derivation),
   `:46` (whole-surface coverage), `:60` (new field not covered is reported), `:52` (mutation).
   Log: `t19_tests.txt` all PASSED; mutation: `coverage_mutation.txt` last line.

2. **Collection-time guard.** `tests/conftest.py:44` is the session-scoped autouse fixture. It
   patches both `.env` readers — the helper (`:57`) and pydantic-settings' own `env_file` (`:66`)
   — then `clear_ambient_environment()` (`:36`) clears the derived names and resets the settings
   cache. It runs after collection, so a module-level `app = create_app()` cannot leave values
   behind. Identical-results test: `tests/integration/test_env_hermeticity.py:136` → PASSED in
   `full_suite_parity.txt`; guard test `tests/unit/test_env_guard.py:69` → PASSED in
   `t19_tests.txt`.

3. **Gate record names the environment.** `src/orchestrator/services/supervisor_service.py:204`
   appends `environment: cwd=…; local .env absent (tracked-files-only worktree); node_modules
   absent; .venv absent` to the existing `tests` check `evidence`; helper at
   `src/orchestrator/services/project_service.py:279`. Tests
   `tests/integration/test_env_hermeticity.py:217` and `:229` read the written
   `verification.json` back and assert `local .env absent` / `present`, and that the check-record
   keys are still exactly `{name,status,command,evidence,notes}`. Both PASSED in
   `t19_tests.txt`. No artifact or check field was added.

4. **Opt-in parity path, separate from gating.** `Makefile:22` (`make test-parity`) and
   `justfile:19` call `scripts/orch_env_parity.sh`, which seeds a copy of the local `.env` into a
   throwaway detached worktree and removes it again. CLI flag `src/orchestrator/cli.py:445`
   (`--env-parity`, help says "NOT the gate"), implemented at
   `src/orchestrator/services/project_service.py:530`, off by default. Tests
   `tests/integration/test_env_hermeticity.py:237` (seed + cleanup), `:255` (no `.env` → nothing
   created), `:268` (opt-in), `:281` (script executed end to end), `:324` (make/just targets).
   All PASSED in `t19_tests.txt`; `tests/integration/test_cli.py` also asserts the help text
   (included in `full_suite.txt`).

5. **Residual fragility gone.** Synthetic `.env` at
   `tests/integration/test_env_hermeticity.py:39`; previously failing pair plus collection-time
   app import in one seeded run at `:158` (generated module `:54`) → PASSED in `t19_tests.txt`;
   mutation at `tests/unit/test_env_guard.py:52` and `coverage_mutation.txt`.

6. **Docs.** `docs/build-loop.md:423` states the tracked-only differences (no `.env`, `.venv`,
   `web/node_modules`, build assets or untracked local config), the hermetic guard, the
   `verification.json` evidence and the developer-only parity path. `README.md:270` adds the
   "Known limitations" line.

## Scope notes

- `runs/<run_id>/` and the verification check-record shape are unchanged; the environment rides
  in the existing `evidence` string.
- No contract file was edited; no dependency was added.
- `--env-parity` is CLI-only; the `/api` doctor route still calls `doctor(project)` and keeps its
  static shape.
