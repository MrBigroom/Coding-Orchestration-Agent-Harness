# T-19 evidence

Source `1e2e522`; this commit changes only evidence. `full_suite.txt`: `615 passed, 6 skipped`;
`lint.txt`: `All checks passed!`. Full map + excerpts: `.orchestrator/handoff.md`.

- DoD 1 derived clear-list: `config.py:154`, `tests/conftest.py:22`, `test_env_guard.py:31,46,52,60`;
  mutation `coverage_mutation.txt`.
- DoD 2 collection-time guard: `tests/conftest.py:44` (autouse), `:36`, `:66`; identical suite with
  and without `.env`: `full_suite_parity.txt`; guard test `test_env_guard.py:69`.
- DoD 3 gate record: `supervisor_service.py:204` extends existing `tests` evidence;
  `test_env_hermeticity.py:217,229` assert it and the frozen five-key shape.
- DoD 4 parity: `Makefile:22`, `justfile:19`, `scripts/orch_env_parity.sh`, `cli.py:445`,
  `project_service.py:530`; `test_env_hermeticity.py:237,255,268,281,324`.
- DoD 5: synthetic `.env` `test_env_hermeticity.py:39`; T-9 pair + collection import `:158`;
  mutation `test_env_guard.py:52`.
- DoD 6: `docs/build-loop.md:423`, `README.md:270`.

No contract file, dependency, run-artifact filename or verification check-field changed.
