"""Shared fixtures: every test gets an isolated ORCHESTRATOR_HOME and no network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.core import config as config_mod
from orchestrator.core import db as db_mod

SECRET_ENV = (
    "ANTHROPIC_API_KEY",
    "QWEN_API_KEY",
    "DEEPSEEK_API_KEY",
    "GLM_API_KEY",
    "HERMES_API_KEY",
    "ORCHESTRATOR_TOKEN",
    # A repo-level `.env` leaks into os.environ as SOON AS any test module imports the app at
    # import time: core/config.get_settings() calls load_env_file(), which load_dotenv()s `.env`
    # into the process environment, and that survives the chdir below. Cleared here so "disabled by
    # default" assertions mean what they say in a checkout that has a `.env`. (Found by the T-9
    # merge: tests/integration/test_notifications.py imports orchestrator.api.main at module level,
    # so a full-suite run in the operator's checkout went red on test_sync_api while every gated
    # run -- a worktree with no `.env`, because .gitignore excludes it -- stayed green.)
    "ORCHESTRATOR_SUPABASE_URL",
    "ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY",
    "ORCHESTRATOR_SUPABASE_EMAIL",
    "ORCHESTRATOR_SUPABASE_PASSWORD",
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "orch-home"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(h))
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_WORKER", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_SCHEDULER", "false")
    monkeypatch.delenv("ORCHESTRATOR_DB_URL", raising=False)
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # keep any repo-level .env out of the settings
    config_mod.reset_settings_cache()
    db_mod.dispose_engines()
    yield h
    db_mod.dispose_engines()
    config_mod.reset_settings_cache()


@pytest.fixture
def initialized(home) -> Path:
    from orchestrator.services import bootstrap

    bootstrap.init()
    return home


@pytest.fixture
def session(initialized):
    with db_mod.session_scope() as s:
        yield s


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, env=env)
    for name, content in (files or {"README.md": "# test\n"}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)
    return path


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
