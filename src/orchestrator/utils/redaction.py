"""Secret redaction for logs, command previews, and LLM prompts."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    # Provider keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # JWT-ish
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    # PEM private keys
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # Bearer tokens in headers
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]{8,}"),
]

# KEY=value / "key": "value" style assignments where the key name looks secret.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>[A-Z0-9_\-]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE[_-]?KEY|ACCESS[_-]?KEY)[A-Z0-9_\-]*)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?!(?-i:[A-Z_][A-Z0-9_]*)(?:[\s"',}]|$))   # an UPPER_CASE env var NAME is not a secret
    (?P<val>[^\s"',}]{4,})
    """
)

# Token *counts* ("inputTokens": 400, max_tokens=16000) are usage numbers, not secrets.
_TOKEN_COUNT_KEY = re.compile(r"(?i)tokens$")

_SECRET_ENV_HINT = re.compile(r"(?i)(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)")


def _redact_assignment(m: re.Match[str]) -> str:
    if m.group("val").isdigit() and _TOKEN_COUNT_KEY.search(m.group("key")):
        return m.group(0)
    return f"{m.group('key')}{m.group('sep')}{REDACTED}"


def _env_secret_values() -> Iterable[str]:
    for k, v in os.environ.items():
        if v and len(v) >= 8 and _SECRET_ENV_HINT.search(k):
            yield v


def redact(text: str | None, extra_secrets: Iterable[str] = ()) -> str:
    if not text:
        return text or ""
    out = text
    for secret in sorted({*extra_secrets, *_env_secret_values()}, key=len, reverse=True):
        if secret and len(secret) >= 8:
            out = out.replace(secret, REDACTED)
    for pat in _PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + REDACTED, out)
        else:
            out = pat.sub(REDACTED, out)
    out = _ASSIGNMENT.sub(_redact_assignment, out)
    return out


SENSITIVE_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env(\..*)?$"),
    re.compile(r"(^|/)[^/]*\.(pem|key|p12|pfx|keystore|jks)$"),
    re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$"),
    re.compile(r"(^|/)\.(netrc|pgpass|npmrc|pypirc)$"),
    re.compile(r"(^|/)credentials(\.json)?$"),
    re.compile(r"(^|/)secrets?\.(json|ya?ml|toml)$"),
)


def is_sensitive_path(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.endswith(".env.example"):
        return False
    return any(r.search(p) for r in SENSITIVE_FILE_PATTERNS)


def strip_sensitive_files_from_diff(diff: str) -> str:
    """Drop hunks for sensitive files (.env, keys) from a unified diff before sending to LLMs."""
    out: list[str] = []
    skip = False
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = line.split()
            path = parts[-1][2:] if len(parts) >= 4 else ""
            skip = is_sensitive_path(path)
            if skip:
                out.append(f"diff --git {parts[-2]} {parts[-1]}\n# [sensitive file omitted]\n")
                continue
        if not skip:
            out.append(line)
    return "".join(out)
