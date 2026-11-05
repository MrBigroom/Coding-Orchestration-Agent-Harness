"""Shared fixtures: every test gets an isolated ORCHESTRATOR_HOME and no network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.core import config as config_mod
from orchestrator.core import db as db_mod
from orchestrator.core.config import PROVIDER_ENV_NAMES, settings_env_names

# Cleared before every test and again once collection has finished. The ORCHESTRATOR_* entries are
# DERIVED from the Settings model (T-19 DoD 1), not hand-kept: a repo-level `.env` leaks into
# os.environ as soon as any test module imports the app at import time (core/config.get_settings()
# calls load_env_file(), which load_dotenv()s `.env`), and that survives the chdir below. The T-9
# merge went red in the operator's checkout and green in every gated worktree because only the first
# world has a `.env`. Provider SDKs read their own variables directly, so those stay explicit.
SECRET_ENV: tuple[str, ...] = (*PROVIDER_ENV_NAMES, *settings_env_names())


def env_names_not_covered(clear_list: tuple[str, ...] | None = None) -> set[str]:
    """Env-visible names the hermeticity guard would leave untouched.

    A pure predicate so the coverage test and its mutation check see the same rule: with the
    hand-kept list (no Settings derivation) the T-9 keys reappear here, which is exactly the drift
    the derivation prevents.
    """
    covered = set(SECRET_ENV if clear_list is None else clear_list)
    return {name for name in (*PROVIDER_ENV_NAMES, *settings_env_names()) if name not in covered}


def clear_ambient_environment() -> None:
    """Make this process look like a tracked-files-only worktree, then drop the settings cache."""
    for name in SECRET_ENV:
        os.environ.pop(name, None)
    config_mod.reset_settings_cache()


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment():
    """Re-apply environment hygiene AFTER collection imports have had their chance.

    A module-level `import orchestrator.api.main` executes `app = create_app()` during collection,
    which calls get_settings() -> load_env_file() while cwd is still the repo. Clearing only inside
    the function-scoped `home` fixture is not enough: anything imported between collection and the
    first test can leave the repo `.env` in os.environ. For the rest of the session a *relative*.
    `.env` is never loaded as ambient configuration — an explicit path (the dotenv unit test) still
    works — so a seeded repo-level .env cannot change an assertion outcome.
    """
    mp = pytest.MonkeyPatch()
    original_load_env_file = config_mod.load_env_file

    def guarded_load_env_file(path=".env") -> None:
        candidate = Path(path)
        if not candidate.is_absolute() and candidate == Path(".env"):
            return
        original_load_env_file(path)

    mp.setattr(config_mod, "load_env_file", guarded_load_env_file)
    # Two readers, both have to be closed: get_settings() calls the helper above for provider
    # keys, and pydantic-settings ALSO reads `env_file=".env"` itself when constructing Settings.
    mp.setitem(config_mod.Settings.model_config, "env_file", None)
    clear_ambient_environment()
    try:
        yield
    finally:
        mp.undo()
        clear_ambient_environment()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "orch-home"
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(h))
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_WORKER", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_SCHEDULER", "false")
    monkeypatch.delenv("ORCHESTRATOR_DB_URL", raising=False)
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
