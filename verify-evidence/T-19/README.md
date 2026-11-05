# T-19 evidence

Source under review: `1e2e522`; this evidence commit changes only `verify-evidence/T-19/`.
`full_suite.txt`: `615 passed, 6 skipped`, exit 0. `lint.txt`: `All checks passed!`.
Full DoD mapping, line numbers and log excerpts are in `.orchestrator/handoff.md`.

- DoD 1 derived clear-list: `src/orchestrator/core/config.py:154`, `tests/conftest.py:22`,
  tests `tests/unit/test_env_guard.py:31,46,52,60`; mutation `coverage_mutation.txt`.
- DoD 2 collection-time guard: `tests/conftest.py:44` (autouse), `:36`, `:66`; suite identical
  with and without `.env`: `full_suite_parity.txt`; guard test `test_env_guard.py:69`.
- DoD 3 gate record: `supervisor_service.py:204` extends the existing `tests` evidence;
  `test_env_hermeticity.py:217,229` assert it and the frozen five-key check shape.
- DoD 4 opt-in parity: `Makefile:22`, `justfile:19`, `scripts/orch_env_parity.sh`,
  `cli.py:445`, `project_service.py:530`; tests `test_env_hermeticity.py:237,255,268,281,324`.
- DoD 5 residual fragility: synthetic `.env` at `test_env_hermeticity.py:39`; T-9 pair +
  collection-time app import at `:158`; mutation at `test_env_guard.py:52`.
- DoD 6 docs: `docs/build-loop.md:423`, `README.md:270`.

No contract file, dependency, run-artifact filename or verification check-field changed.
