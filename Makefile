# coding-orchestrator — common tasks. See also `justfile` (same targets).
SHELL := /bin/bash
VENV  := .venv/bin
PORT  ?= $(or $(ORCHESTRATOR_BIND_PORT),8321)
FILE  ?= memory-export.json

.PHONY: setup dev start test lint format doctor demo build clean memory-export memory-import

setup:          ## venv + deps (+ npm if present), init home, alembic upgrade head
	./scripts/setup.sh

dev:            ## backend --reload on :8321 + Vite on :5173
	./scripts/dev.sh

start:          ## production backend on :8321 (serves web/dist or the fallback UI)
	$(VENV)/orchestrator serve --port $(PORT)

test:           ## pytest (offline; external CLI tests are skipped)
	$(VENV)/pytest

lint:           ## ruff check
	$(VENV)/ruff check src tests

format:         ## ruff format
	$(VENV)/ruff format src tests

doctor:
	$(VENV)/orchestrator doctor

demo:
	./scripts/demo.sh

build:          ## build the React GUI
	./scripts/build.sh

clean:          ## remove build/test caches (never touches ORCHESTRATOR_HOME data)
	rm -rf web/dist .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -not -path "./web/node_modules/*" -exec rm -rf {} +

memory-export:  ## make memory-export FILE=backup.json
	$(VENV)/orchestrator memory export -o $(FILE)

memory-import:  ## make memory-import FILE=backup.json
	$(VENV)/orchestrator memory import $(FILE)
