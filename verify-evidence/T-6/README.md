# T-6 verification evidence

Task **T-6 — "Verification commands cannot run in a worktree: no venv bootstrap, and editable
installs import the main checkout"** · branch
`task-6-verification-commands-cannot-run-in-a-worktree-n` · project **Coding Orchestrator**
(base branch `main` @ `91d6ee7`).

This directory exists because `.orchestrator/` is gitignored and is **not** archived into
`runs/<run_id>/`, so evidence written there is invisible to the review gate. Everything here is
committed on the branch and is reachable with `git show <sha>:verify-evidence/T-6/...`.
Outputs use the `.txt` suffix because `*.log` is gitignored.

## DoD item → evidence

| DoD | What it requires | Evidence in this directory |
| --- | --- | --- |
| 1 | A committed wrapper that is path-independent: resolves the worktree root via `git rev-parse --show-toplevel` and the main repo via `git rev-parse --path-format=absolute --git-common-dir`, uses the main repo venv interpreter by absolute path, sets `PYTHONPATH` to the worktree `src`, ignores the inherited `VIRTUAL_ENV`, prints the resolved module path and test root, exits non-zero when the import resolves outside the worktree | `scripts/orch_verify.sh` (committed mode `100755`, pinned by `tests/integration/test_verify_wrapper.py::test_this_repo_commits_the_wrapper_executable`); the printed lines are in every log here |
| 2 | Works from a clean worktree created by `git worktree add`, proving `orchestrator.__file__` is under the worktree, with no network/keys/installs | `dod2_fresh_worktree.sh` → `dod2_fresh_worktree.txt` |
| 3 | `project doctor --run-commands` (opt-in, default off) runs the configured command in the existing throwaway-worktree mechanism and reports exit code **and** whether the import resolved inside that worktree | `dod3_doctor_live.sh` → `dod3_doctor_live.txt` (real CLI, isolated `ORCHESTRATOR_HOME=/tmp/t6-dod3-home`) plus `tests/integration/test_verify_wrapper.py::test_doctor_*` and `tests/integration/test_cli.py::test_project_doctor_run_commands_flag` |
| 4 | Docs: `docs/build-loop.md` + `docs/configuration.md` state commands must be path-independent and document the wrapper; README no longer implies `make test` runs anywhere but the main checkout; the null-`test_command` consequence at `supervisor_service.py` l.134-146 is documented next to the command fields | `docs/build-loop.md`, `docs/configuration.md`, `README.md` diffs; `docs/contract-change-request-T6-orch-verify-marker.md` (status **PROPOSED**, awaiting operator decision) |
| 5 | Offline tests with a temp git repo + fake package installed editable in a throwaway venv covering (a) trap reproduced without the wrapper, (b) resolves inside with the wrapper, (c) guard trips loudly, (d) doctor reports the failure; `make test`/`make lint` pass through the wrapper | `tests/integration/test_verify_wrapper.py` (`test_without_the_wrapper_the_import_resolves_outside_the_worktree`, `test_wrapper_resolves_inside_the_worktree_and_stops_the_false_pass`, `test_wrapper_guard_trips_when_the_resolution_is_wrong`, `test_doctor_reports_the_trap_when_the_command_is_not_path_independent`); `make_test.txt`, `make_lint.txt`, `dod5_targeted_tests.txt` |

## Logs and what they show

* `make_test.txt` — `./scripts/orch_verify.sh test` (what `make test` runs) in **this** worktree:
  marker line naming this worktree's `src`, then `395 passed, 6 skipped`, exit 0.
* `make_lint.txt` — `./scripts/orch_verify.sh lint` (`make lint`): `All checks passed!`, exit 0.
* `dod2_fresh_worktree.txt` — `git worktree add --detach <tmp> main` → no `.venv`, no wrapper
  (the wrapper ships with this branch, so the literal run against `main` is pending operator
  acceptance after merge; `git rev-list --left-right --count main...HEAD` is `0 14` and
  `git merge-tree --write-tree main HEAD` returns HEAD's own tree, i.e. merging is a
  fast-forward and the content is identical). Unwrapped, the main venv interpreter imports the
  **main checkout** (the trap). Detached at this branch's commit in the same worktree: the
  wrapper resolves `orchestrator.__file__` **under the worktree**, a single-file pytest run
  (17 passed), `lint` (exit 0) and the **full suite** (395 passed, 6 skipped, exit 0) all run
  against the worktree content, and both guard cases exit **3**.
* `dod3_doctor_live.txt` — the real CLI with `PYTHONPATH` = this worktree's `src` and
  `ORCHESTRATOR_HOME=/tmp/t6-dod3-home`: (A) a venv-relative command exits 0 in the throwaway
  worktree **but is flagged** `FAIL test resolves inside the worktree — no ORCH_VERIFY marker`;
  (B) the wrapper command exits 0 **and** resolves inside the worktree for test and lint;
  (C) plain `doctor` creates no worktree and runs nothing; (D) with `base_branch=main` the
  probe reports the wrapper missing instead of going green (pre-merge state); (E) outside a
  repo the wrapper exits 4; (F) no throwaway worktree or `.managed` marker is left behind.
* `dod5_targeted_tests.txt` — the T-6 regression tests plus the previously load-sensitive chat
  driver test: `20 passed`, exit 0.

## Reproducing

```bash
cd "$(git rev-parse --show-toplevel)"
make test                                   # or: ./scripts/orch_verify.sh test
make lint                                   # or: ./scripts/orch_verify.sh lint
bash verify-evidence/T-6/dod2_fresh_worktree.sh   # ~1 min, writes its own .txt
bash verify-evidence/T-6/dod3_doctor_live.sh      # ~30 s, writes its own .txt
```

Nothing here touches the network, needs an API key, or installs a package. Scripts clean up
their throwaway worktrees (`/tmp/t6-dod2-proof`, `/tmp/t6-dod3-home`) on exit and `git worktree
list` in the main repo is left showing only the main checkout and the task worktree.
