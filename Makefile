# coding-orchestrator — common tasks. See also `justfile` (same targets).
# test/lint go through scripts/orch_verify.sh so they also work in a git worktree.
SHELL := /bin/bash
VENV  := .venv/bin
PORT  ?= $(or $(ORCHESTRATOR_BIND_PORT),8321)
FILE  ?= memory-export.json

.PHONY: setup dev start test test-parity lint verify format doctor demo build clean memory-export memory-import desktop desktop-install desktop-uninstall

setup:          ## venv + deps (+ npm if present), init home, alembic upgrade head
	./scripts/setup.sh

dev:            ## backend --reload on :8321 + Vite on :5173
	./scripts/dev.sh

start:          ## production backend on :8321 (serves web/dist or the fallback UI)
	$(VENV)/orchestrator serve --port $(PORT)

test:           ## pytest via scripts/orch_verify.sh (works in the main checkout and in worktrees)
	./scripts/orch_verify.sh test

test-parity:    ## DEVELOPER check, not the gate: suite in a throwaway worktree with your local .env
	./scripts/orch_env_parity.sh test

lint:           ## ruff check via scripts/orch_verify.sh (same path-independence checks)
	./scripts/orch_verify.sh lint

verify:         ## tests + lint in one path-independent run
	./scripts/orch_verify.sh all

format:         ## ruff format (main checkout only: it rewrites this working tree)
	$(VENV)/ruff format src tests

doctor:
	$(VENV)/orchestrator doctor

demo:
	./scripts/demo.sh

build:          ## build the React GUI
	./scripts/build.sh

desktop:        ## open the GUI in a native window (starts or attaches to the server)
	$(VENV)/orchestrator desktop

desktop-install:   ## install pywebview, build the GUI, add the app-menu launcher + icon
	./scripts/desktop-install.sh

desktop-uninstall: ## remove the launcher + icon
	$(VENV)/orchestrator desktop uninstall

clean:          ## remove build/test caches (never touches ORCHESTRATOR_HOME data)
	rm -rf web/dist .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -not -path "./web/node_modules/*" -exec rm -rf {} +

memory-export:  ## make memory-export FILE=backup.json
	$(VENV)/orchestrator memory export -o $(FILE)

memory-import:  ## make memory-import FILE=backup.json
	$(VENV)/orchestrator memory import $(FILE)
