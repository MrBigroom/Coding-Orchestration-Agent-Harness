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

# DERIVED from the Settings model (T-19 DoD 1), not hand-kept: a repo-level `.env` leaks into
# os.environ when a test module imports the app at import time (get_settings() -> load_env_file()
# -> load_dotenv), and that survives the chdir below. Provider SDKs read their own variables
# directly, so those stay explicit.
AMBIENT_ENV: tuple[str, ...] = (*PROVIDER_ENV_NAMES, *settings_env_names())

# The environment before any test module was imported. Collection-time `.env` reads export
# arbitrary keys (OPENAI_*, ANTHROPIC_BASE_URL, proxies), so restoring this baseline — not just
# AMBIENT_ENV — is what makes "a repo-level .env is inert" true.
_ENVIRON_AT_IMPORT: dict[str, str] = dict(os.environ)


def env_names_not_covered(
    clear_list: tuple[str, ...] | None = None,
    model: type | None = None,
) -> set[str]:
    """Env-visible names the hermeticity guard would leave untouched (model-aware, so an aliased
    field counts). Callers can override the clear-list to model the old hand-kept shape."""
    if clear_list is not None:
        covered = set(clear_list)
    elif model is None:
        covered = set(AMBIENT_ENV)
    else:
        covered = set((*PROVIDER_ENV_NAMES, *settings_env_names(model)))
    visible = set(PROVIDER_ENV_NAMES) | set(settings_env_names(model))
    return visible - covered


def restore_import_environment() -> None:
    """Undo everything a collection-time `.env` read exported into this process.

    Only keys absent at conftest import are dropped and existing values are restored. `PYTEST_*`
    is preserved: pytest writes its per-test bookkeeping var and pops it on teardown.
    """
    for name in [
        n for n in os.environ if n not in _ENVIRON_AT_IMPORT and not n.startswith("PYTEST_")
    ]:
        os.environ.pop(name, None)
    for name, value in _ENVIRON_AT_IMPORT.items():
        if os.environ.get(name) != value:
            os.environ[name] = value


def clear_ambient_environment() -> None:
    """Make this process look like a tracked-files-only worktree, then drop the settings cache.

    Matching is case-insensitive: pydantic matches ORCHESTRATOR_* that way, so a lowercase key in
    `.env` is still ambient configuration.
    """
    targets = {name.lower() for name in AMBIENT_ENV}
    for name in [n for n in os.environ if n.lower() in targets]:
        os.environ.pop(name, None)
    config_mod.reset_settings_cache()


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment():
    """Re-apply environment hygiene AFTER collection imports have had their chance.

    A module-level `import orchestrator.api.main` executes create_app() during collection, which can
    load the repo `.env` while cwd is still the repo. This restores the import-time environment and
    closes both readers (get_settings()'s helper and pydantic-settings' `env_file`), so a
    cwd-relative `.env` is never ambient for the rest of the session; an explicit path still works.
    """
    mp = pytest.MonkeyPatch()
    original_load_env_file = config_mod.load_env_file

    def guarded_load_env_file(path=".env") -> None:
        candidate = Path(path)
        if not candidate.is_absolute() and candidate == Path(".env"):
            return
        original_load_env_file(path)

    mp.setattr(config_mod, "load_env_file", guarded_load_env_file)
    mp.setitem(config_mod.Settings.model_config, "env_file", None)
    restore_import_environment()
    clear_ambient_environment()
    try:
        yield
    finally:
        mp.undo()
        restore_import_environment()
        clear_ambient_environment()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "orch-home"
    known = {name.lower() for name in AMBIENT_ENV}
    for name in [n for n in os.environ if n.lower() in known]:
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
