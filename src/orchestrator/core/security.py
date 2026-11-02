"""Local token auth and env-var hygiene for child processes."""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Query, WebSocket, status
from fastapi.datastructures import Headers
from fastapi.responses import JSONResponse

from orchestrator.core.config import LOCAL_HOSTS, get_settings

TOKEN_HEADER = "X-Orchestrator-Token"  # noqa: S105 - header name, not a secret

# Minimal env passed to every child process; adapters add their declared vars on top.
BASE_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "SHELL",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
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


def _hostname(authority: str) -> str:
    return (urlsplit(f"//{authority}").hostname or "").lower()


def same_origin_ok(headers: Mapping[str, str]) -> bool:
    """Block other websites from reaching this server.

    A page on any site can send requests and open WebSockets to 127.0.0.1, and DNS rebinding
    can point a hostile domain at this server. So the Host must be local (unless a token is
    configured, which such a page can't know), and a browser's Origin must match that Host.
    Clients outside a browser send no Origin."""
    host = headers.get("host", "")
    origin = headers.get("origin")
    s = get_settings()
    try:
        if not s.token and _hostname(host) not in {*LOCAL_HOSTS, s.bind_host}:
            return False
        return origin is None or urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:  # malformed Host or Origin
        return False


class SameOriginMiddleware:
    """Apply same_origin_ok to every HTTP request and WebSocket, before any route runs."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket") or same_origin_ok(Headers(scope=scope)):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await WebSocket(scope, receive, send).close(code=4403)
        else:
            refused = {"error": "cross-origin request refused", "detail": None}
            await JSONResponse(status_code=403, content=refused)(scope, receive, send)


def child_env(
    extra_names: list[str] | None = None, overrides: dict[str, str] | None = None
) -> dict[str, str]:
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
