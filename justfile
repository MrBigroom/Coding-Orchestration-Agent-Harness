# coding-orchestrator — same targets as the Makefile. `just --list`
venv := ".venv/bin"
port := env_var_or_default("ORCHESTRATOR_BIND_PORT", "8321")

setup:
    ./scripts/setup.sh

dev:
    ./scripts/dev.sh

start:
    {{venv}}/orchestrator serve --port {{port}}

test *ARGS:
    {{venv}}/pytest {{ARGS}}

lint:
    {{venv}}/ruff check src tests

format:
    {{venv}}/ruff format src tests

doctor:
    {{venv}}/orchestrator doctor

demo *ARGS:
    ./scripts/demo.sh {{ARGS}}

build:
    ./scripts/build.sh

desktop:
    {{venv}}/orchestrator desktop

desktop-install:
    ./scripts/desktop-install.sh

desktop-uninstall:
    {{venv}}/orchestrator desktop uninstall

clean:
    rm -rf web/dist .pytest_cache .ruff_cache build dist *.egg-info
    find . -name __pycache__ -type d -prune -not -path "./web/node_modules/*" -exec rm -rf {} +

memory-export file="memory-export.json":
    {{venv}}/orchestrator memory export -o {{file}}

memory-import file="memory-export.json":
    {{venv}}/orchestrator memory import {{file}}
