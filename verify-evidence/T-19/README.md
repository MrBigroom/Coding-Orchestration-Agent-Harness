# T-19 evidence

Source under review: `1e2e522` (evidence commit adds only this directory). `full_suite.txt` shows
`615 passed, 6 skipped`, exit 0; `lint.txt` shows `All checks passed!`.

DoD map (full detail + exact log lines are in `.orchestrator/handoff.md`):

1. Derived clear-list: `src/orchestrator/core/config.py:154`, `tests/conftest.py:22`; tests
   `tests/unit/test_env_guard.py:31,46,52,60` PASSED in `t19_tests.txt`; mutation
   `coverage_mutation.txt`.
2. Collection-time guard: `tests/conftest.py:44` (session autouse), `:36` (clear+reset), `:66`
   (pydantic `env_file` closed); `tests/unit/test_env_guard.py:69` PASSED; identical counts with
   and without `.env`: `full_suite_parity.txt`.
3. Gate record: `src/orchestrator/services/supervisor_service.py:204` appends the environment to
   the existing `tests` evidence; `tests/integration/test_env_hermeticity.py:217,229` PASSED and
   assert the frozen five-key check shape.
4. Opt-in parity: `Makefile:22`, `justfile:19`, `scripts/orch_env_parity.sh`,
   `src/orchestrator/cli.py:445`, `src/orchestrator/services/project_service.py:530`; tests
   `tests/integration/test_env_hermeticity.py:237,255,268,281,324` PASSED.
5. Residual fragility: synthetic `.env` at `tests/integration/test_env_hermeticity.py:39`; T-9
   pair + collection-time app import at `:158` PASSED; mutation at
   `tests/unit/test_env_guard.py:52`.
6. Docs: `docs/build-loop.md:423`, `README.md:270`.

No contract file, dependency, run-artifact filename or verification check-field changed.
