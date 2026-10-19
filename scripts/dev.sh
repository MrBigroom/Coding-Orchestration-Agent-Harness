#!/usr/bin/env bash
# Backend with reload on :8321 + Vite dev server on :5173 (if npm is available).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${ORCHESTRATOR_BIND_PORT:-8321}"

.venv/bin/orchestrator serve --port "$PORT" --reload &
BACK=$!
trap 'kill $BACK 2>/dev/null || true' EXIT INT TERM

if command -v npm >/dev/null 2>&1 && [ -d web/node_modules ]; then
  echo "GUI (dev): http://127.0.0.1:5173  ·  API: http://127.0.0.1:$PORT/api/docs"
  (cd web && npm run dev)
else
  echo "Vite not available; GUI served by backend at http://127.0.0.1:$PORT/"
  wait $BACK
fi
