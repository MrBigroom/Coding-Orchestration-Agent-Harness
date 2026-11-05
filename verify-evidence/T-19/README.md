# T-19 evidence (code HEAD c812467)

Regenerate with `./verify-evidence/T-19/capture.sh` from the repo root. Every `.txt` starts with
`# HEAD=<sha> tracked-dirty-entries=<n>` and the command that produced it. The evidence commit
changes only these paths; the code under test is c812467.

| DoD | Where it lives (file:line at c812467) | Proof |
|---|---|---|
| 1. derive the clear-list from Settings | `src/orchestrator/core/config.py:154` `settings_env_names()` asks pydantic-settings' own `EnvSettingsSource`; `:22` `PROVIDER_ENV_NAMES` is the only hand-kept part; `tests/conftest.py:20` `AMBIENT_ENV`; `:28` `env_names_not_covered()` | `t19_tests.txt`: `test_the_real_settings_surface_is_fully_covered` (`tests/unit/test_env_guard.py:57`) PASSED — asserts `env_names_not_covered() == set()` for the real `Settings`; `test_removing_the_settings_derivation_fails_the_coverage_check` (`:98`) PASSED against the same predicate; `coverage_mutation.txt:3-4` `clean with the derivation: True` / `after removing it: False`; `:8` `MUTATION_DETECTED` |
| 2. collection-time guard; `.env` inert | `tests/conftest.py:25` import-time snapshot, `:44` `restore_import_environment()`, `:72` session autouse fixture | `t19_tests.txt`: `test_affected_subset_is_identical_with_and_without_a_seeded_dotenv` (`tests/integration/test_env_hermeticity.py:169`) PASSED (same counts, seeded vs not); `test_seeded_dotenv_changes_the_outcome_without_the_guard` (`:191`) PASSED (control really flips); `test_restoring_the_import_environment_drops_arbitrary_dotenv_keys` (`tests/unit/test_env_guard.py:112`) PASSED |
| 3. environment named in the gate record | `src/orchestrator/services/project_service.py:280` `environment_summary()`; `src/orchestrator/services/supervisor_service.py:204` | `t19_tests.txt`: `test_verification_json_names_the_gated_environment` (`tests/integration/test_env_hermeticity.py:267`) PASSED — `local .env absent`, no worktree label for a plain checkout, saved check keys still the frozen five |
| 4. opt-in parity path, never gating | `Makefile:22`, `justfile:19`, `scripts/orch_env_parity.sh`, `src/orchestrator/cli.py:445`, `src/orchestrator/services/project_service.py:571` `run_env_parity_checks()`, `:459` `_seeded_env_copy()` | `make_test_parity.txt:2` `DEVELOPER CHECK (not the gate command)`; `t19_tests.txt`: `test_doctor_env_parity_seeds_the_local_env_and_removes_the_tree` (`:293`), `test_env_parity_rejects_a_command_without_an_orch_verify_marker` (`:394`) PASSED; round-4 finding 4 (a pre-existing/foreign `.env`): `test_env_parity_reports_a_preexisting_dotenv_instead_of_raising` (`tests/integration/test_env_hermeticity.py:336`) and `test_env_parity_never_unlinks_a_dotenv_it_did_not_create` (`:360`) PASSED |
| 5. residual fragility gone offline | synthetic `.env` built from pairs at `tests/integration/test_env_hermeticity.py:41`; pair + collection-time import at `:207` (`:91` is the generated module); subset at `:169` | `t19_tests.txt`: `test_previously_failing_pair_and_collection_import_pass_with_a_seeded_dotenv` PASSED; `coverage_mutation.txt:8` |
| 6. docs | `docs/build-loop.md:423`; `README.md:270` | see the committed files; this README is the artifact index |

Full suite: `full_suite.txt:4` `615 passed, 9 skipped`. Lint: `lint.txt:4` `All checks passed!`.
The 3 spawn-a-pytest tests are skipped by default (DoD 2) and are run opt-in in `t19_tests.txt`
(18 passed, including the two round-5 finding-4 tests).
Scope note: a killed `--env-parity` run's residue is REPORTED, not swept — that is T-20; the
in-run seeded `.env` is created `0600` and removed before its own tree is deleted.