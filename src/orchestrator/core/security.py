"""Local token auth and env-var hygiene for child processes."""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Query, status

from orchestrator.core.config import get_settings

TOKEN_HEADER = "X-Orchestrator-Token"

# Minimal env passed to every child process; adapters add their declared vars on top.
BASE_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "VIRTUAL_ENV",
)


def token_ok(supplied: str | None) -> bool:
    expected = get_settings().token
    if not expected:
        return True
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def require_token(
    x_orchestrator_token: str | None = Header(default=None, alias=TOKEN_HEADER),
) -> None:
    if not token_ok(x_orchestrator_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token")


def require_token_ws(token: str | None = Query(default=None)) -> bool:
    return token_ok(token)


def child_env(extra_names: list[str] | None = None,
              overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment: allowlisted basics + explicitly declared variable names."""
    env: dict[str, str] = {}
    for name in (*BASE_ENV_ALLOWLIST, *(extra_names or [])):
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    if overrides:
        env.update(overrides)
    return env


def env_is_set(name: str | None) -> bool:
    return bool(name) and bool(os.environ.get(name or ""))
