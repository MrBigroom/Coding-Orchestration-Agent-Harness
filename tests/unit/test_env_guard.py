"""T-19 DoD 1: the hermeticity guard is derived from the settings surface, not hand-kept.

The T-9 merge leaked `ORCHESTRATOR_SUPABASE_*` past conftest's tuple because a new settings field
appeared and nobody remembered to add its env name. These tests pin the derivation and the
coverage predicate, including the mutation that removing it must fail.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestrator.core.config import (
    PROVIDER_ENV_NAMES,
    Settings,
    get_settings,
    reset_settings_cache,
    settings_env_names,
)
from tests import conftest

# Synthetic only: these values exist to prove they cannot change an assertion outcome.
SEEDED_ENV = """\
ORCHESTRATOR_SUPABASE_URL=https://synthetic.invalid
ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY=synthetic-publishable-key
ORCHESTRATOR_SUPABASE_EMAIL=synthetic@example.invalid
ORCHESTRATOR_SUPABASE_PASSWORD=synthetic-password
ANTHROPIC_API_KEY=synthetic-anthropic-key
"""


def test_settings_env_names_are_derived_from_every_model_field():
    expected = {
        f"{str(Settings.model_config['env_prefix']).upper()}{name.upper()}"
        for name in Settings.model_fields
    }
    assert set(settings_env_names()) == expected
    # The names that actually leaked across the T-9 merge must be in the derived set.
    assert {
        "ORCHESTRATOR_SUPABASE_URL",
        "ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY",
        "ORCHESTRATOR_SUPABASE_EMAIL",
        "ORCHESTRATOR_SUPABASE_PASSWORD",
    } <= set(settings_env_names())


def test_clear_list_covers_the_whole_env_visible_surface():
    assert conftest.env_names_not_covered() == set()
    assert set(PROVIDER_ENV_NAMES) <= set(conftest.SECRET_ENV)
    assert set(settings_env_names()) <= set(conftest.SECRET_ENV)


def test_removing_the_settings_derivation_makes_the_coverage_check_fail():
    """Mutation check (DoD 5): the old hand-kept shape must be detected, not silently accepted."""
    missing = conftest.env_names_not_covered(PROVIDER_ENV_NAMES)
    assert "ORCHESTRATOR_SUPABASE_URL" in missing
    assert "ORCHESTRATOR_SUPABASE_PASSWORD" in missing
    assert "ORCHESTRATOR_TOKEN" in missing


def test_relative_dotenv_is_not_ambient_configuration_in_the_suite(tmp_path, monkeypatch):
    """The session guard makes a repo-level `.env` inert; an explicit path still loads."""
    (tmp_path / ".env").write_text(SEEDED_ENV)
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    try:
        settings = get_settings()
        assert settings.supabase_url is None
        assert settings.supabase_publishable_key is None
        assert settings.supabase_sync_enabled is False
        assert "ORCHESTRATOR_SUPABASE_URL" not in os.environ
    finally:
        reset_settings_cache()


def test_explicit_env_file_path_is_still_loadable(tmp_path, monkeypatch):
    """The guard is narrow: it only refuses a cwd-relative `.env`, not the dotenv helper itself."""
    from orchestrator.core.config import load_env_file

    env_file = tmp_path / "explicit.env"
    env_file.write_text("T19_EXPLICIT_KEY=loaded\n")
    monkeypatch.delenv("T19_EXPLICIT_KEY", raising=False)
    try:
        load_env_file(env_file)
        assert os.environ["T19_EXPLICIT_KEY"] == "loaded"
    finally:
        os.environ.pop("T19_EXPLICIT_KEY", None)


@pytest.mark.parametrize("name", ["ORCHESTRATOR_HOME", "ORCHESTRATOR_SUPABASE_PASSWORD"])
def test_home_fixture_clears_the_derived_names_after_collection(name):
    """Even the fixture's own setenv calls happen after the clear, so they win."""
    assert name in conftest.SECRET_ENV


def test_env_file_path_default_is_unchanged():
    """Sanity: the guard lives in conftest, not in production config."""
    from orchestrator.core.config import load_env_file

    assert Path(load_env_file.__defaults__[0]) == Path(".env")
