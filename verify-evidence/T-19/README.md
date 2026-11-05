# T-19 evidence — gated runs execute in a different environment

All captures are stamped with the code HEAD they ran at. The source under review is
`f9e3248e7295b8898c4ed1822d785f6c1f5987c5`; `verify-evidence/T-19/` itself is committed
afterwards and changes no source or test file.

Files in this directory:

| File | What it proves |
|---|---|
| `full_suite.txt` | Full suite at the code HEAD: `618 passed, 6 skipped in 368.73s`, exit 0. Line 20. |
| `lint.txt` | `ruff check src tests`: `All checks passed!`, exit 0. Line 11. |
| `full_suite_parity.txt` | Named run of DoD 2's two-suite test: `1 passed, 623 deselected` (line 22). The test itself runs the full suite twice in a throwaway worktree of HEAD — once without a seeded `.env`, once with one — and asserts identical counts. |
| `t19_tests.txt` | Named (`-vv`) run of the new T-19 tests in `tests/unit/test_env_guard.py` and `tests/integration/test_env_hermeticity.py` (full-suite parity test deselected because it is captured separately above): `17 passed` (line 37). |
| `coverage_mutation.txt` | Mutation check: with the OLD hand-kept clear-list (provider keys only), the coverage predicate reports all 42 derived `ORCHESTRATOR_*` names as gaps and the script prints `MUTATION_DETECTED` (line 5). |
| `capture.sh` | The capture helper used for the suite/lint/unit/integration/mutation files. |

## DoD self-check mapped to evidence

1. **SECRET_ENV derived from the Settings model.** `src/orchestrator/core/config.py:154`
   (`settings_env_names()` derives `<env_prefix><FIELD>` from `Settings.model_fields`);
   `tests/conftest.py:22` builds `SECRET_ENV = (*PROVIDER_ENV_NAMES, *settings_env_names())`.
   Coverage tests: `tests/unit/test_env_guard.py:34`, `:49`; new-field drift test `:63`.
   Log: `t19_tests.txt` lines for `test_settings_env_names_are_derived_from_every_model_field`,
   `test_clear_list_covers_the_whole_env_visible_surface`,
   `test_a_new_env_visible_field_that_is_not_covered_is_reported` — all `PASSED`.
   Mutation: `coverage_mutation.txt:5`.

2. **Collection-time guard.** `tests/conftest.py:44` is the session-scoped autouse
   `_hermetic_environment`; it patches both readers — the `load_env_file` helper (`:63`) and
   pydantic-settings' own `env_file` (`:66`) — then calls `clear_ambient_environment()` (`:36`),
   which clears the derived list and calls `reset_settings_cache()`. It runs after collection
   imports, so a module-level `app = create_app()` cannot leave `.env` values behind.
   Identical-results test: `tests/integration/test_env_hermeticity.py:161`, captured in
   `full_suite_parity.txt` as `PASSED` (line 20) with the suite result line 22.
   Guard behaviour test: `tests/unit/test_env_guard.py:72` (`PASSED`).

3. **Gate record names the environment.** `src/orchestrator/services/supervisor_service.py:204`
   appends `environment: cwd=…; local .env absent (tracked-files-only worktree); node_modules
   absent; .venv absent` to the existing `tests` check's `evidence`; helper at
   `src/orchestrator/services/project_service.py:279`. Tests
   `tests/integration/test_env_hermeticity.py:261` and `:277` read the written
   `verification.json` back and assert `local .env absent` / `local .env present` respectively,
   and that the check-record keys are still exactly the frozen five. Both `PASSED` in
   `t19_tests.txt`. No new artifact or check field was added.

4. **Opt-in parity path, separate from gating.** `Makefile:22` (`make test-parity`) and
   `justfile:19` call `scripts/orch_env_parity.sh`, which creates a throwaway detached worktree,
   copies the local `.env`, runs `scripts/orch_verify.sh`, and removes the tree. The CLI flag is
   `src/orchestrator/cli.py:445` (`--env-parity`, help says "NOT the gate"), implemented at
   `src/orchestrator/services/project_service.py:530` (`run_env_parity_checks`), off by default.
   Tests: `tests/integration/test_env_hermeticity.py:289` (seeds and cleans up), `:314`
   (no `.env` creates nothing), `:331` (opt-in), `:348` (actually executes the script end to end
   and asserts the seeded `.env` is visible to the test), `:408` (targets documented). All
   `PASSED` in `t19_tests.txt`. `tests/integration/test_cli.py` also asserts the help text names
   `--env-parity` and "NOT the gate" (included in `full_suite.txt`).

5. **Residual fragility proven gone offline.** Synthetic `.env` constant at
   `tests/integration/test_env_hermeticity.py:41`; previously failing pair plus module-level app
   import in one seeded run at `:184` (generated collection-time test at `:70`), `PASSED` in
   `t19_tests.txt`; mutation check at `tests/unit/test_env_guard.py:55` and
   `coverage_mutation.txt:5`.

6. **Docs.** `docs/build-loop.md:423` ("Gated runs are environment-free — and the opt-in parity
   check") states the tracked-only differences (no `.env`, `.venv`, `web/node_modules`, build
   assets or untracked local config), the hermetic guard, the `verification.json` evidence and
   the developer-only parity path. `README.md:270` adds the one-line "Known limitations" entry.

## Scope notes

- `runs/<run_id>/` and the `verification.json` check-record shape were not changed; the
  environment is recorded inside the existing `evidence` string.
- No contract file was edited. No dependency was added.
- The parity flag is CLI-only; the `/api` doctor route still calls `doctor(project)` and keeps
  its static shape.