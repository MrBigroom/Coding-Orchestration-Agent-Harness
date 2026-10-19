#!/usr/bin/env bash
# Create the venv, install backend (+ frontend if npm exists), init ORCHESTRATOR_HOME,
# and apply migrations. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if command -v uv >/dev/null 2>&1; then
  [ -d .venv ] || uv venv -q .venv
  uv pip install -q -p .venv -e ".[dev]"
else
  [ -d .venv ] || "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e ".[dev]"
fi

if command -v npm >/dev/null 2>&1; then
  (cd web && npm install --no-audit --no-fund)
else
  echo "npm not found: skipping frontend install (the fallback UI will be served)."
fi

[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example (fill in keys you need)"; }

.venv/bin/orchestrator init     # creates ORCHESTRATOR_HOME, runs `alembic upgrade head`, seeds defaults
echo "setup complete. Next: make demo  |  make start"
