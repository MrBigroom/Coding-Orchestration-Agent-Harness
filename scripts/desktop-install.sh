#!/usr/bin/env bash
# Install the desktop app for the current user: pywebview (GTK) into the venv, a fresh GUI
# build, and the app-menu launcher + icon under ~/.local/share. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d .venv ] || { echo "run ./scripts/setup.sh first"; exit 1; }
if command -v uv >/dev/null 2>&1; then
  uv pip install -q -p .venv -e ".[dev,desktop]"
else
  .venv/bin/pip install -q -e ".[dev,desktop]"
fi

if command -v npm >/dev/null 2>&1; then
  ./scripts/build.sh
else
  echo "npm not found: the desktop app will show the fallback UI."
fi

.venv/bin/orchestrator desktop install
echo "done. Open \"Coding Orchestrator\" from your app launcher, or run: make desktop"
