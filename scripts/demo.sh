#!/usr/bin/env bash
# Offline demo: mock provider + mock harness + examples/sample-project. No keys, no network.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/orchestrator demo "$@"
