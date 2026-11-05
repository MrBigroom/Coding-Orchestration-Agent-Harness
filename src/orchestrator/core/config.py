"""Runtime configuration loaded from environment variables and `.env`.

All settings use the ORCHESTRATOR_ prefix. Paths default to subdirectories of
ORCHESTRATOR_HOME, which itself defaults to $XDG_DATA_HOME/coding-orchestrator.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, AliasPath, Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Provider SDKs read their own variables directly (not through `Settings`), so these are the only
# env names the hermeticity guard still has to keep by hand. Everything with the ORCHESTRATOR_
# prefix is derived from the model below, because a hand-kept list drifted behind the settings
# surface once already (T-19: the T-9 merge leaked ORCHESTRATOR_SUPABASE_* past SECRET_ENV).
PROVIDER_ENV_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "QWEN_API_KEY",
    "DEEPSEEK_API_KEY",
    "GLM_API_KEY",
    "HERMES_API_KEY",
)


def default_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "coding-orchestrator"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    home: Path = Field(default_factory=default_home)
    db_url: str | None = None
    bind_host: str = "127.0.0.1"
    bind_port: int = 8321
    token: str | None = None
    log_level: str = "INFO"

    max_parallel_runs: int = 2
    max_parallel_runs_per_project: int = 1
    enable_scheduler: bool = True
    enable_worker: bool = True

    worktree_root: Path | None = None
    artifact_root: Path | None = None
    log_root: Path | None = None
    memory_root: Path | None = None
    run_retention_days: int = 30

    git_user_name: str = "coding-orchestrator"
    git_user_email: str = "orchestrator@localhost"
    default_base_branch: str = "main"

    frontend_dist: Path | None = None
    autostart_browser: bool = False

    merge_enabled: bool = False
    auto_merge_enabled: bool = False
    supervisor_microfixes_enabled: bool = False

    max_prompt_context_tokens: int = 60_000
    max_log_tokens: int = 4_000
    max_diff_tokens: int = 12_000

    # Memory budgets (per injected section, ~4 chars/token). Memory is static, so
    # `enforce_budget` can never trim it: uncapped growth squeezes diff/handoff/verification
    # evidence instead. These caps are enforced inside `memory_service.for_prompt`.
    max_memory_tokens_global: int = 2_000
    max_memory_tokens_role: int = 800
    max_memory_tokens_project: int = 4_000
    # Warn (prompt preview, `memory budget`, triage) once a section reaches this share of
    # its cap, so the operator can condense before anything is demoted.
    memory_budget_warn_fraction: float = 0.8
    # Set false to inject every approved entry (pre-budget behaviour).
    memory_budget_enabled: bool = True

    # Use Anthropic's server-side refusal fallback ("default" routing) on Claude 5.x models.
    anthropic_refusal_fallback: bool = True
    # Let GUI/API trigger project commands (test/lint/build) directly. Off by default.
    allow_gui_commands: bool = False
    # Let the GUI start interactive harness sessions, which run agents with shell access.
    allow_interactive_sessions: bool = False
    allow_unattended_sessions: bool = False

    # Optional Supabase synchronization. Credentials stay device-local in the environment.
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    supabase_email: str | None = None
    supabase_password: str | None = None
    supabase_sync_batch_size: int = Field(default=100, gt=0, le=1000)
    supabase_sync_enabled: bool = False
    supabase_sync_interval_seconds: int = Field(default=60, ge=15, le=86400)

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        self.home = Path(self.home).expanduser()
        self.worktree_root = Path(self.worktree_root or self.home / "worktrees").expanduser()
        self.artifact_root = Path(self.artifact_root or self.home / "runs").expanduser()
        self.log_root = Path(self.log_root or self.home / "logs").expanduser()
        self.memory_root = Path(self.memory_root or self.home / "memory").expanduser()
        if not self.db_url:
            self.db_url = f"sqlite:///{self.home / 'orchestrator.db'}"
        return self

    @property
    def build_log_path(self) -> Path:
        return self.home / "build-log.jsonl"

    @property
    def chats_root(self) -> Path:
        return self.home / "chats"

    @property
    def agent_outputs_root(self) -> Path:
        return self.home / "agent-outputs"

    @property
    def evidence_root(self) -> Path:
        """Out-of-band submissions the harness must never discard (T-10 DoD 3).

        A `runs/<run_id>/` artifact layout is a frozen contract, so a handoff found in a worktree
        at the START of a round — before any run of this round exists to hold it — is archived
        here instead of into a new file name under `runs/`.
        """
        return self.home / "evidence"

    @property
    def is_local_bind(self) -> bool:
        return self.bind_host in LOCAL_HOSTS

    def ensure_dirs(self) -> None:
        for p in (
            self.home,
            self.worktree_root,
            self.artifact_root,
            self.log_root,
            self.memory_root,
        ):
            Path(p).mkdir(parents=True, exist_ok=True)


def _validation_alias_names(field: FieldInfo) -> list[str]:
    """Env names a field's `validation_alias` accepts, flattened the way pydantic reads them."""
    alias = field.validation_alias
    if alias is None:
        return []
    if isinstance(alias, str):
        return [alias]
    if not isinstance(alias, (AliasChoices, AliasPath)):
        return []
    converted = alias.convert_to_aliases()
    if not converted:
        return []
    if isinstance(converted[0], list):  # AliasChoices: one candidate per choice
        return [str(choice[0]) for choice in converted if choice]
    return [str(converted[0])]  # AliasPath: only the head names an env var


def settings_env_names(model: type[BaseSettings] | None = None) -> tuple[str, ...]:
    """Every env var pydantic-settings would read for :class:`Settings` (or `model`).

    Derived from the model's fields and config, never hand-maintained, including `validation_alias`,
    `case_sensitive` and `env_prefix_target` — the previous prefix+UPPER shape ignored those, so an
    aliased field escaped the guard entirely (T-19 DoD 1). Canonical names are upper-case when
    matching is case-insensitive; with `case_sensitive=True` they are exactly what pydantic reads.
    """
    cls = model or Settings
    cfg = dict(cls.model_config)
    prefix = str(cfg.get("env_prefix") or "")
    target = cfg.get("env_prefix_target", "variable")
    case_sensitive = bool(cfg.get("case_sensitive", False))
    by_alias = cfg.get("validate_by_alias") is not False
    by_name = cfg.get("validate_by_name")
    if by_name is None:
        by_name = cfg.get("populate_by_name")
    if by_name is None:
        by_name = cfg.get("validate_by_alias") is False
    alias_prefix = prefix if target in ("alias", "all") else ""
    var_prefix = prefix if target in ("variable", "all") else ""
    names: list[str] = []
    for field_name, field in cls.model_fields.items():
        aliases = _validation_alias_names(field) if by_alias else []
        candidates = [f"{alias_prefix}{alias}" for alias in aliases]
        if not aliases or by_name:
            candidates.append(f"{var_prefix}{field_name}")
        for candidate in candidates:
            canonical = candidate if case_sensitive else candidate.upper()
            if canonical not in names:
                names.append(canonical)
    return tuple(names)


def load_env_file(path: str | Path = ".env") -> None:
    """Export provider keys etc. from .env into os.environ (never overriding real env vars).
    pydantic-settings only maps ORCHESTRATOR_* fields; providers read their own variables."""
    from dotenv import load_dotenv

    if Path(path).is_file():
        load_dotenv(path, override=False)


@lru_cache
def get_settings() -> Settings:
    load_env_file()
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
