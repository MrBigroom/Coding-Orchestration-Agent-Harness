"""Secret redaction for logs, command previews, and LLM prompts."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class RedactionHit:
    """One pattern class that rewrote text, and how many substitutions it made.

    Recorded in the reviewer payload's truncation manifest (T-16 DoD 1). It deliberately carries
    no match text: the whole point is to make a rewrite visible without reprinting the secret.
    """

    pattern_class: str
    count: int


@dataclass(frozen=True)
class RedactionReport:
    text: str
    hits: tuple[RedactionHit, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.hits)


# Named so a manifest row can say WHICH class matched without exposing the matched value. The
# names are stable enough for a reviewer to correlate with this file.
_NAMED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Provider keys. The lookbehind anchor is load-bearing (T-8 DoD 10): without it these patterns
    # match INSIDE ordinary words that end in "sk" followed by a hyphen, so branch/task/phase
    # names were destroyed by redact() — "task-8-recovery-commits..." lost its value while a
    # findings_json payload still parsed, i.e. a machine consumer silently read a WRONG branch
    # name. A real key starts where no letter/digit precedes it (start of text, after whitespace,
    # `=`, `:`, a quote, `_` or `-`) and still redacts; "task-", "risk-", "mask-" no longer match,
    # because the preceding letter blocks the lookbehind. `\b` was NOT used: `_` is a word
    # character, so `\bsk-` would miss `TOKEN_sk-proj-…` and `API_KEY_sk-ant-…` — shapes secrets
    # really do appear in — which the DoD forbids ("keep every existing credential pattern
    # redacting exactly as before"). This anchor is `\b` plus that case, never narrower.
    ("sk-ant", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("sk", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("gh", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("glpat", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("xox", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b")),
    ("aws-akia", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-aiza", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # JWT-ish
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ),
    # PEM private keys
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
        ),
    ),
    # Bearer tokens in headers
    (
        "bearer",
        re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]{8,}"),
    ),
]

# KEY=value / "key": "value" style assignments where the key name looks secret. The
# lookbehind and the bounded suffix keep matching linear on long unbroken text: harness logs
# can hold megabytes of it, and this runs on the event loop.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![A-Z0-9_\-])
    (?P<key>[A-Z0-9_\-]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE[_-]?KEY|ACCESS[_-]?KEY)[A-Z0-9_\-]{0,64})
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?!(?-i:[A-Z_][A-Z0-9_]*)(?:[\s"',}]|$))   # an UPPER_CASE env var NAME is not a secret
    (?P<val>[^\s"',}]{4,})
    """
)

# Token *counts* ("inputTokens": 400, max_tokens=16000) are usage numbers, not secrets.
_TOKEN_COUNT_KEY = re.compile(r"(?i)tokens$")
# A count can be followed by sentence punctuation: `max_diff_tokens=12000;` is the exact shape
# that reached a reviewer as `max_diff_tokens=[REDACTED]` (T-16 DoD 2). The punctuation belongs to
# the prose, not to the number, so strip it before deciding whether the value is a count.
_TRAILING_PROSE = ".;:!?)]}"
_TOKEN_COUNT_VALUE = re.compile(r"[\d][\d,_]*$")
# Self-describing placeholders are not credentials. This is deliberately narrow: the value must
# BE one of these words/prefixes, followed by a non-alphanumeric delimiter or end of value, so a
# real random secret that merely starts with the same letters is still redacted. This fixes the
# R-52 fixture in which a test asserted `ANTHROPIC_API_KEY=synthetic-not-a-secret` and the
# reviewer was shown `ANTHROPIC_API_KEY=[REDACTED]`, i.e. the redactor destroyed the evidence
# that no secret existed. Every recognised credential *shape* below still redacts first, so
# `KEY=synthetic-sk-ant-...` loses the key regardless of this exemption.
_PLACEHOLDER_VALUE = re.compile(
    r"""(?ix)^(?:
        \[REDACTED\]
      | <[^<>\s]*>
      | \$\{?[A-Za-z_][A-Za-z0-9_]*\}?
      | (?:synthetic|placeholder|dummy|fake|example|sample|redacted|changeme|not[-_]?a[-_]?secret)
         (?:$|[^A-Za-z0-9].*)
      | https?://[^\s/]*\.(?:invalid|example(?:\.(?:com|org|net))?)(?:/\S*)?
      | [^\s@]+@[^\s@]*\.(?:invalid|example(?:\.(?:com|org|net))?)
    )$"""
)

_SECRET_ENV_HINT = re.compile(r"(?i)(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)")


def _is_benign_assignment(m: re.Match[str]) -> bool:
    """True when a `key=value` match is a usage number or a self-declared placeholder."""
    key, val = m.group("key"), m.group("val")
    if _TOKEN_COUNT_KEY.search(key):
        trimmed = val.rstrip(_TRAILING_PROSE)
        if _TOKEN_COUNT_VALUE.fullmatch(trimmed):
            return True
    return bool(_PLACEHOLDER_VALUE.match(val))


def _redact_assignment(m: re.Match[str]) -> str:
    if _is_benign_assignment(m):
        return m.group(0)
    return f"{m.group('key')}{m.group('sep')}{REDACTED}"


def _env_secret_values() -> Iterable[str]:
    for k, v in os.environ.items():
        if v and len(v) >= 8 and _SECRET_ENV_HINT.search(k):
            yield v


def redact_with_report(
    text: str | None, extra_secrets: Iterable[str] = ()
) -> RedactionReport:
    """Redact and report which pattern classes matched, without recording any match text.

    `redact()` is the same operation without the report; keeping one implementation means the
    manifest can never describe a rewrite that did not happen (or miss one that did).
    """
    if not text:
        return RedactionReport(text or "")
    out = text
    counts: dict[str, int] = {}
    for secret in sorted({*extra_secrets, *_env_secret_values()}, key=len, reverse=True):
        if secret and len(secret) >= 8:
            n = out.count(secret)
            if n:
                out = out.replace(secret, REDACTED)
                counts["environment-value"] = counts.get("environment-value", 0) + n
    for name, pat in _NAMED_PATTERNS:
        if pat.groups:
            out, n = pat.subn(lambda m: m.group(1) + REDACTED, out)
        else:
            out, n = pat.subn(REDACTED, out)
        if n:
            counts[name] = counts.get(name, 0) + n

    def counted_assignment(m: re.Match[str]) -> str:
        if _is_benign_assignment(m):
            return m.group(0)
        counts["assignment"] = counts.get("assignment", 0) + 1
        return f"{m.group('key')}{m.group('sep')}{REDACTED}"

    out = _ASSIGNMENT.sub(counted_assignment, out)
    hits = tuple(RedactionHit(name, n) for name, n in sorted(counts.items()) if n)
    return RedactionReport(out, hits)


def redact(text: str | None, extra_secrets: Iterable[str] = ()) -> str:
    return redact_with_report(text, extra_secrets).text


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


def strip_sensitive_files_from_diff(diff: str, dropped: list[str] | None = None) -> str:
    """Drop hunks for sensitive files (.env, keys) from a unified diff before sending to LLMs.

    `dropped` (optional) collects the path of every file whose content was withheld, so the caller
    can record it as a truncation instead of letting the file disappear between the patch and the
    prompt (T-11 DoD 4). The path itself is still shown — only the body is withheld.
    """
    out: list[str] = []
    skip = False
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = line.split()
            path = parts[-1][2:] if len(parts) >= 4 else ""
            skip = is_sensitive_path(path)
            if skip:
                if dropped is not None:
                    dropped.append(path)
                out.append(f"diff --git {parts[-2]} {parts[-1]}\n# [sensitive file omitted]\n")
                continue
        if not skip:
            out.append(line)
    return "".join(out)
