# sample-project

A tiny project used by `orchestrator demo`. The demo copies this directory into
`$ORCHESTRATOR_HOME/sample-project`, initialises it as a git repository on `main`, and
registers it with:

- test command: `python3 run_tests.py`
- lint command: none
- base branch: `main`

Nothing here needs network access or third-party packages.
