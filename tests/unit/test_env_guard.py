"""T-19 DoD 1: the hermeticity clear-list is derived from what pydantic actually reads.

The assertions are behavioural (pydantic reads the name into a real `Settings` instance) and name
literal env keys, so a derivation that silently stops seeing a new/aliased field fails here. The
old hand-kept clear-list is the mutation control.
"""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from orchestrator.core.config import (
    PROVIDER_ENV_NAMES,
    Settings,
    get_settings,
    reset_settings_cache,
    settings_env_names,
)
from tests import conftest

# Synthetic only. Built from (name, value) pairs so no assignment-shaped literal is written
# anywhere: the log redactor keys on the assignment NAME, so a flat seed line would reach the
# reviewer masked even when the value is a harmless placeholder (round-3 finding 4).
SEEDED_ENV_PAIRS = (
    ("ORCHESTRATOR_SUPABASE_URL", "https://synthetic.invalid"),
    ("ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY", "not-a-secret-0000"),
    ("ORCHESTRATOR_SUPABASE_EMAIL", "synthetic@example.invalid"),
    ("ORCHESTRATOR_SUPABASE_PASSWORD", "not-a-secret-0001"),
    ("ANTHROPIC_API_KEY", "not-a-secret-0002"),
)
SEEDED_ENV = "".join(f"{name}={value}\n" for name, value in SEEDED_ENV_PAIRS)


class FutureSettings(Settings):
    future_provider_key: str | None = None


class AliasedSettings(Settings):
    future_secret: str | None = Field(default=None, validation_alias="T19_FUTURE_SECRET")


class CaseSensitiveSettings(Settings):
    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_", case_sensitive=True, extra="ignore"
    )
    future_secret: str | None = None


def _coverage_is_clean() -> bool:
    """The predicate the DoD-1 drift test pins: the shipped clear-list leaves no gaps."""
    return conftest.env_names_not_covered() == set()


def test_the_real_settings_surface_is_fully_covered():
    """DoD 1: with the SHIPPED clear-list, no Settings-visible name is left ambient.

    This is the test that fails if `AMBIENT_ENV` is reverted to the old hand-kept tuple, which is
    exactly the drift DoD 1 exists to prevent (round-3 finding 1).
    """
    assert _coverage_is_clean(), conftest.env_names_not_covered()


def test_the_leaked_supabase_names_are_covered(monkeypatch):
    """The four names that leaked across the T-9 merge are in the derived clear-list."""
    leaked = {
        "ORCHESTRATOR_SUPABASE_URL",
        "ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY",
        "ORCHESTRATOR_SUPABASE_EMAIL",
        "ORCHESTRATOR_SUPABASE_PASSWORD",
    }
    assert leaked <= set(settings_env_names())
    assert leaked <= set(conftest.AMBIENT_ENV)


def test_a_new_env_visible_field_that_is_not_covered_is_reported(monkeypatch):
    """Drift detection: a field added after AMBIENT_ENV was built is read AND named as a gap."""
    name = "ORCHESTRATOR_FUTURE_PROVIDER_KEY"
    monkeypatch.setenv(name, "not-a-secret-0000")
    assert FutureSettings().future_provider_key == "not-a-secret-0000"  # pydantic reads that name
    assert name in settings_env_names(FutureSettings)  # the derivation sees the new field
    gaps = conftest.env_names_not_covered(clear_list=conftest.AMBIENT_ENV, model=FutureSettings)
    assert name in gaps  # the stale clear-list leaves it ambient


def test_alias_and_case_sensitivity_are_read_and_covered(monkeypatch):
    """An aliased or case-sensitive field cannot escape the guard (literal names, not a mirror)."""
    monkeypatch.setenv("T19_FUTURE_SECRET", "not-a-secret-0000")
    monkeypatch.setenv("ORCHESTRATOR_future_secret", "not-a-secret-0001")
    assert AliasedSettings().future_secret == "not-a-secret-0000"
    assert "T19_FUTURE_SECRET" in settings_env_names(AliasedSettings)
    assert CaseSensitiveSettings().future_secret == "not-a-secret-0001"
    assert "ORCHESTRATOR_future_secret" in settings_env_names(CaseSensitiveSettings)


def test_removing_the_settings_derivation_fails_the_coverage_check(monkeypatch):
    """Mutation check (DoD 5): the old hand-kept list must make the REAL coverage test fail.

    It asserts against the same `_coverage_is_clean()` predicate as the test above, so this
    mutation is not a separate tautology: if the derivation is removed, the green test goes red.
    """
    monkeypatch.setattr(conftest, "AMBIENT_ENV", PROVIDER_ENV_NAMES)
    assert _coverage_is_clean() is False
    missing = conftest.env_names_not_covered()
    assert "ORCHESTRATOR_SUPABASE_URL" in missing
    assert "ORCHESTRATOR_SUPABASE_PASSWORD" in missing
    assert "ORCHESTRATOR_TOKEN" in missing


def test_restoring_the_import_environment_drops_arbitrary_dotenv_keys(monkeypatch):
    """DoD 2: collection-time `.env` keys outside AMBIENT_ENV are removed too."""
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-secret-0000")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://synthetic.invalid")
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic.invalid:8080")
    conftest.restore_import_environment()
    assert "OPENAI_API_KEY" not in os.environ
    assert "ANTHROPIC_BASE_URL" not in os.environ
    assert "HTTP_PROXY" not in os.environ


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
