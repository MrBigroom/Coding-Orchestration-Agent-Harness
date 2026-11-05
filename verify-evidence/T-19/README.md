# T-19 evidence

Regenerate with `./verify-evidence/T-19/capture.sh` from the repo root. Every `.txt` starts with
`# HEAD=<sha> tracked-dirty-entries=<n>` and the command that produced it. This directory was
regenerated at code HEAD `c3e114a`; committing the evidence does not change that code.

| DoD | Where it lives (file:line) | Proof line |
|---|---|---|
| 1. derive the clear-list | `src/orchestrator/core/config.py:154` `settings_env_names()` asks pydantic-settings' own `EnvSettingsSource` (`:164`); `tests/conftest.py:20` `AMBIENT_ENV`, `:28` `env_names_not_covered()` | `t19_tests.txt`: `test_the_leaked_supabase_names_are_covered` (`tests/unit/test_env_guard.py:49`), `test_a_new_env_visible_field_that_is_not_covered_is_reported` (`:61`), `test_alias_and_case_sensitivity_are_read_and_covered` (`:71`), `test_removing_the_settings_derivation_makes_the_coverage_check_fail` (`:81`) PASSED; `coverage_mutation.txt:8` `MUTATION_DETECTED` |
| 2. collection-time guard / `.env` cannot change an assertion | `tests/conftest.py:25` import-time snapshot, `:44` `restore_import_environment()`, `:72` session autouse `_hermetic_environment`, `:89-90` both readers closed | `affected_subset.txt:4` `test_affected_subset_is_identical_with_and_without_a_seeded_dotenv` (`tests/integration/test_env_hermeticity.py:158`) PASSED (same counts with/without the seeded `.env`); `t19_tests.txt`: `test_seeded_dotenv_changes_the_outcome_without_the_guard` (`:180`) and `test_restoring_the_import_environment_drops_arbitrary_dotenv_keys` (`tests/unit/test_env_guard.py:91`) PASSED |
| 3. environment in the gate record (no new artifact/field) | `src/orchestrator/services/supervisor_service.py:204`; `src/orchestrator/services/project_service.py:279` `environment_summary()` | `t19_tests.txt`: `test_verification_json_names_the_gated_environment` (`tests/integration/test_env_hermeticity.py:256`) PASSED — asserts `local .env absent`, no worktree label for a plain checkout, and saved check keys are still the frozen five |
| 4. opt-in parity path, never a gate command | `Makefile:22`, `justfile:19`, `scripts/orch_env_parity.sh`, `src/orchestrator/cli.py:445`, `src/orchestrator/services/project_service.py:549` | `t19_tests.txt`: `test_doctor_env_parity_seeds_the_local_env_and_removes_the_tree` (`tests/integration/test_env_hermeticity.py:282`), `test_env_parity_sweeps_a_leftover_seeded_worktree` (`:302`), `test_make_target_is_documented_as_the_developer_parity_path` (`:317`) PASSED; `make_test_parity.txt:2` `DEVELOPER CHECK (not the gate command)` and `:5` `7 passed` |
| 5. residual fragility gone offline | synthetic `.env` at `tests/integration/test_env_hermeticity.py:39`; pair + collection-time app import at `:196` (`:72`/`:80` is the generated module); affected subset at `:158` | `t19_tests.txt`: `test_previously_failing_pair_and_collection_import_pass_with_a_seeded_dotenv` PASSED; `test_affected_subset_is_identical_with_and_without_a_seeded_dotenv` PASSED; `test_removing_the_settings_derivation_makes_the_coverage_check_fail` PASSED; `coverage_mutation.txt:8` |
| 6. docs | `docs/build-loop.md:423` (tracked-only world, hermetic guard, gate record, developer parity, spawn-test rule); `README.md:270` known limitation | — |

Full suite at this HEAD: `full_suite.txt:4` `611 passed, 9 skipped` (the 3 spawn-a-pytest tests are
deliberately skipped by default; they are run opt-in in `t19_tests.txt` and `affected_subset.txt`).
Lint: `lint.txt:4` `All checks passed!`.

Evidence is intentionally small: `t19_tests.txt` names all 14 T-19 tests under
`ORCH_ENV_SPAWN_TESTS=1`, and `affected_subset.txt` is the DoD 2 parity proof. The branch diff
(below the reviewer's 12k-token budget only before this directory is added) is a separate,
known budget issue; see the handoff.