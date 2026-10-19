#!/usr/bin/env bash
# Build the React GUI into web/dist (served by `orchestrator serve`).
set -euo pipefail
cd "$(dirname "$0")/../web"
if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found: cannot build the React GUI. The backend serves the fallback UI instead."
  exit 0
fi
[ -d node_modules ] || npm install --no-audit --no-fund
npm run build
echo "built web/dist"
