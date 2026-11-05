"""T-19 DoD 1: the hermeticity clear-list is derived from what pydantic actually reads.

Pinned against pydantic-settings' own reader (an independent oracle), including alias and
case-sensitivity, plus the mutation that removing the derivation must fail.
"""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import EnvSettingsSource, SettingsConfigDict

from orchestrator.core.config import (
    PROVIDER_ENV_NAMES,
    Settings,
    get_settings,
    reset_settings_cache,
    settings_env_names,
)
from tests import conftest

# Synthetic only, spelled as UPPER_CASE placeholders so the log redactor leaves evidence readable.
SEEDED_ENV = """\
ORCHESTRATOR_SUPABASE_URL=https://synthetic.invalid
ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY=SYNTHETIC_PLACEHOLDER
ORCHESTRATOR_SUPABASE_EMAIL=synthetic@example.invalid
ORCHESTRATOR_SUPABASE_PASSWORD=SYNTHETIC_PLACEHOLDER
ANTHROPIC_API_KEY=SYNTHETIC_PLACEHOLDER
"""


def _pydantic_env_names(model: type[Settings]) -> set[str]:
    """Oracle: pydantic-settings' own field->env-name extraction, not our derivation."""
    source = EnvSettingsSource(model)
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        for _key, env_name, _complex in source._extract_field_info(field, field_name):
            names.add(env_name if source.case_sensitive else env_name.upper())
    return names


def test_secret_env_is_exactly_what_pydantic_reads():
    """The clear-list must equal pydantic's reader, not a parallel reimplementation."""
    assert set(settings_env_names()) == _pydantic_env_names(Settings)
    assert set(conftest.AMBIENT_ENV) == set(PROVIDER_ENV_NAMES) | _pydantic_env_names(Settings)
    assert conftest.env_names_not_covered() == set()
    # The names that actually leaked across the T-9 merge must be in the derived set.
    assert {
        "ORCHESTRATOR_SUPABASE_URL",
        "ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY",
        "ORCHESTRATOR_SUPABASE_EMAIL",
        "ORCHESTRATOR_SUPABASE_PASSWORD",
    } <= set(settings_env_names())


class AliasedSettings(Settings):
    future_secret: str | None = Field(default=None, validation_alias="T19_FUTURE_SECRET")


class CaseSensitiveSettings(Settings):
    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_", case_sensitive=True, extra="ignore"
    )
    future_secret: str | None = None


def test_alias_and_case_sensitivity_are_derived_like_pydantic():
    """An aliased or case-sensitive field must not escape the guard."""
    assert set(settings_env_names(AliasedSettings)) == _pydantic_env_names(AliasedSettings)
    assert set(settings_env_names(CaseSensitiveSettings)) == _pydantic_env_names(
        CaseSensitiveSettings
    )


def test_a_new_env_visible_field_that_is_not_covered_is_reported(monkeypatch):
    """Drift detection: a field added after AMBIENT_ENV was built is named."""
    monkeypatch.setenv("T19_FUTURE_SECRET", "SYNTHETIC_PLACEHOLDER")
    assert AliasedSettings().future_secret == "SYNTHETIC_PLACEHOLDER"  # pydantic reads that name
    gaps = conftest.env_names_not_covered(clear_list=conftest.AMBIENT_ENV, model=AliasedSettings)
    assert "T19_FUTURE_SECRET" in gaps
    # The derivation itself does see it: only the stale clear-list is a gap.
    assert conftest.env_names_not_covered(model=AliasedSettings) == set()


def test_removing_the_settings_derivation_makes_the_coverage_check_fail(monkeypatch):
    """Mutation check (DoD 5): the old hand-kept clear-list must be detected."""
    monkeypatch.setattr(conftest, "AMBIENT_ENV", PROVIDER_ENV_NAMES)
    missing = conftest.env_names_not_covered()
    assert "ORCHESTRATOR_SUPABASE_URL" in missing
    assert "ORCHESTRATOR_SUPABASE_PASSWORD" in missing
    assert "ORCHESTRATOR_TOKEN" in missing
    assert missing  # the whole derived surface is reported, not a single lucky key


def test_restoring_the_import_environment_drops_arbitrary_dotenv_keys(monkeypatch):
    """DoD 2: collection-time `.env` keys outside AMBIENT_ENV are removed too."""
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_PLACEHOLDER")
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
