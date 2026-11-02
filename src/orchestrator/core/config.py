"""Runtime configuration loaded from environment variables and `.env`.

All settings use the ORCHESTRATOR_ prefix. Paths default to subdirectories of
ORCHESTRATOR_HOME, which itself defaults to $XDG_DATA_HOME/coding-orchestrator.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
