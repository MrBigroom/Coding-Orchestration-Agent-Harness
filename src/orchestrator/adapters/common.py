"""Helpers shared by the harness adapters: token-usage parsing, final-text extraction for
JSON output modes, and the OpenAI-compatible endpoint settings handed to harnesses that
the orchestrator configures per run (Pi, DeepSeek Harness, Qwen Code).

Usage is normalised to one shape, whatever the harness prints:

  {"input_tokens": all prompt tokens, cached included,
   "cache_read_input_tokens": prompt tokens served from the provider's cache,
   "uncached_input_tokens": input_tokens - cache_read_input_tokens,
   "output_tokens": ..., "requests": LLM calls seen (when the harness reports them)}
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from orchestrator.adapters.base import AdapterResult, RunContext
from orchestrator.core.schemas import Handoff
from orchestrator.utils.json_output import JSONExtractionError, parse_model

# Keep in sync with services.provider_service.TOKEN_PLAN_BASE_URL (adapters don't import
# services).
TOKEN_PLAN_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"  # noqa: S105

# Default endpoint capability for harnesses we configure: Model Studio / Qwen Cloud.
# Override per adapter via capabilities.provider in the GUI/CLI.
DEFAULT_PROVIDER: dict[str, Any] = {
    "base_url_env": "QWEN_OPENAI_BASE_URL",
    "base_url_default": TOKEN_PLAN_BASE_URL,
    "api_key_env": "QWEN_API_KEY",
}

# Models that accept explicit `cache_control` markers on Model Studio / Qwen Cloud
# (hit at 10% of input price vs 20% for the implicit cache). DeepSeek V4.x and GLM are
# implicit-only there, so markers are never sent for them.
DEFAULT_EXPLICIT_CACHE_MODELS = ["qwen3.8-max", "qwen3.8-flash"]


def provider_settings(capabilities: dict[str, Any]) -> dict[str, Any] | None:
    """Endpoint the harness should call, or None to leave the harness's own config alone."""
    prov = capabilities.get("provider", DEFAULT_PROVIDER)
    if not prov:
        return None
    base_url = os.environ.get(prov.get("base_url_env") or "") or prov.get("base_url_default")
    if not base_url:
        return None
    return {"base_url": base_url, "api_key_env": prov.get("api_key_env") or "QWEN_API_KEY"}


def wants_explicit_cache(model: str | None, capabilities: dict[str, Any]) -> bool:
    if not model:
        return False
    prefixes = capabilities.get("explicit_cache_models", DEFAULT_EXPLICIT_CACHE_MODELS)
    return any(model.startswith(p) for p in prefixes)


# ---------------------------------------------------------------- output parsing


def _json_lines(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _json_doc(text: str) -> Any:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        # Persisted logs pass through secret redaction. Keys containing "token" can
        # therefore gain an unquoted [REDACTED] value (notably ZCode totalTokenCount).
        try:
            return json.loads(text.strip().replace("[REDACTED]", "null"))
        except json.JSONDecodeError:
            return None


def _usage(inp: int, cached: int, out: int, requests: int | None, total_input: bool) -> dict:
    """`total_input` says whether `inp` already includes the cached tokens."""
    total = inp if total_input else inp + cached
    usage = {
        "input_tokens": total,
        "cache_read_input_tokens": cached,
        "uncached_input_tokens": total - cached,
        "output_tokens": out,
    }
    if requests is not None:
        usage["requests"] = requests
    return usage


def _n(v: Any) -> int:
    return int(v) if isinstance(v, int | float) else 0


def _qwen(text: str) -> dict | None:
    doc = _json_doc(text)
    events = doc if isinstance(doc, list) else list(_json_lines(text))
    for e in reversed(events):
        if isinstance(e, dict) and e.get("type") == "result" and isinstance(e.get("usage"), dict):
            u = e["usage"]
            reqs = None
            for m in (e.get("stats") or {}).get("models", {}).values():
                reqs = (reqs or 0) + _n((m.get("api") or {}).get("totalRequests"))
            return _usage(
                _n(u.get("input_tokens")),
                _n(u.get("cache_read_input_tokens")),
                _n(u.get("output_tokens")),
                reqs,
                total_input=True,
            )
    return None


def _zcode(text: str) -> dict | None:
    doc = _json_doc(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("usage"), dict):
        return None
    u = doc["usage"]
    return _usage(
        _n(u.get("inputTokens")),
        _n(u.get("cacheReadTokens")),
        _n(u.get("outputTokens")),
        _n(u.get("modelRequestCount")) or None,
        total_input=True,
    )


def _claude(text: str) -> dict | None:
    doc = _json_doc(text)
    events = doc if isinstance(doc, list) else [doc] if isinstance(doc, dict) else []
    events = events or list(_json_lines(text))
    for e in reversed(events):
        if isinstance(e, dict) and e.get("type") == "result" and isinstance(e.get("usage"), dict):
            u = e["usage"]
            return _usage(
                _n(u.get("input_tokens")) + _n(u.get("cache_creation_input_tokens")),
                _n(u.get("cache_read_input_tokens")),
                _n(u.get("output_tokens")),
                _n(e.get("num_turns")) or None,
                total_input=False,
            )
    return None


def _pi(text: str) -> dict | None:
    inp = cached = out = reqs = 0
    for e in _json_lines(text):
        if e.get("type") != "message_end":
            continue
        msg = e.get("message") or {}
        u = msg.get("usage")
        if msg.get("role") != "assistant" or not isinstance(u, dict):
            continue
        inp += _n(u.get("input")) + _n(u.get("cacheWrite"))
        cached += _n(u.get("cacheRead"))
        out += _n(u.get("output"))
        reqs += 1
    return _usage(inp, cached, out, reqs, total_input=False) if reqs else None


def _dsh(text: str) -> dict | None:
    inp = cached = out = reqs = 0
    for e in _json_lines(text):
        u = e.get("usage")
        if e.get("type") == "status" and e.get("phase") == "step_end" and isinstance(u, dict):
            inp += _n(u.get("inputTokens"))
            cached += _n(u.get("cacheReadTokens"))
            out += _n(u.get("outputTokens"))
            reqs += 1
    return _usage(inp, cached, out, reqs, total_input=False) if reqs else None


def _opencode(text: str) -> dict | None:
    inp = cached = out = reqs = 0
    for e in _json_lines(text):
        tokens = (e.get("part") or {}).get("tokens")
        if e.get("type") == "step_finish" and isinstance(tokens, dict):
            cache = tokens.get("cache") or {}
            inp += _n(tokens.get("input")) + _n(cache.get("write"))
            cached += _n(cache.get("read"))
            out += _n(tokens.get("output"))
            reqs += 1
    return _usage(inp, cached, out, reqs, total_input=False) if reqs else None


_AIDER = re.compile(
    r"Tokens: ([\d.]+)([kM]?) sent(?:, ([\d.]+)([kM]?) cache write)?"
    r"(?:, ([\d.]+)([kM]?) cache hit)?, ([\d.]+)([kM]?) received"
)


def _aider_num(value: str | None, suffix: str | None) -> int:
    if not value:
        return 0
    return round(float(value) * {"k": 1_000, "M": 1_000_000}.get(suffix or "", 1))


def _aider(text: str) -> dict | None:
    inp = cached = out = reqs = 0
    for m in _AIDER.finditer(text):
        inp += _aider_num(m.group(1), m.group(2))
        cached += _aider_num(m.group(5), m.group(6))
        out += _aider_num(m.group(7), m.group(8))
        reqs += 1
    # Aider's "sent" is the prompt total; numbers above 1k are rounded by Aider itself.
    return _usage(inp, min(cached, inp), out, reqs, total_input=True) if reqs else None


USAGE_PARSERS = {
    "qwen_code": _qwen,
    "zcode": _zcode,
    "claude_cli": _claude,
    "pi": _pi,
    "deepseek_harness": _dsh,
    "opencode": _opencode,
    "aider": _aider,
}


def parse_usage(kind: str, stdout: str) -> dict[str, Any] | None:
    """Token usage a harness printed, normalised; None when it printed none we understand."""
    parser = USAGE_PARSERS.get(kind)
    if parser is None or not stdout:
        return None
    try:
        return parser(stdout)
    except (TypeError, ValueError, AttributeError):
        return None


def final_text(kind: str, stdout: str) -> str | None:
    """The agent's final message from a JSON output mode, for the handoff fallback."""
    if kind in ("qwen_code", "claude_cli"):
        doc = _json_doc(stdout)
        events = doc if isinstance(doc, list) else [doc] if isinstance(doc, dict) else []
        for e in reversed(events or list(_json_lines(stdout))):
            if isinstance(e, dict) and e.get("type") == "result":
                return e.get("result") if isinstance(e.get("result"), str) else None
    if kind == "zcode":
        doc = _json_doc(stdout)
        if isinstance(doc, dict) and isinstance(doc.get("response"), str):
            return doc["response"]
    if kind == "pi":
        for e in reversed(list(_json_lines(stdout))):
            msg = e.get("message") or {}
            if e.get("type") == "message_end" and msg.get("role") == "assistant":
                parts = msg.get("content") or []
                return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if kind == "deepseek_harness":
        for e in reversed(list(_json_lines(stdout))):
            if e.get("type") == "final":
                for key in ("text", "message", "content", "answer"):
                    if isinstance(e.get(key), str):
                        return e[key]
    if kind == "opencode":
        texts = [
            (e.get("part") or {}).get("text", "")
            for e in _json_lines(stdout)
            if e.get("type") == "text"
        ]
        return "".join(t for t in texts if isinstance(t, str)) or None
    return None


# ---------------------------------------------------------------- adapter mixins


class JsonOutputMixin:
    """For adapters whose stdout is a JSON event stream: when no handoff file was written,
    look for the handoff JSON in the agent's final message rather than in raw stdout."""

    kind: str

    def parse_result(self, ctx: RunContext, result: AdapterResult) -> Handoff | None:
        handoff = super().parse_result(ctx, result)  # type: ignore[misc]
        if handoff is not None or not (result.stdout_path and result.stdout_path.is_file()):
            return handoff
        text = final_text(self.kind, result.stdout_path.read_text(errors="replace"))
        if not text:
            return None
        try:
            return parse_model(text, Handoff)
        except JSONExtractionError:
            return None


class ManagedConfigMixin:
    """For adapters that need a per-run config file: `prepare(ctx, tmp)` writes it into a
    temporary directory (deleted after the run, never under the artifacts dir) and returns
    extra environment variables for the child. Secrets are referenced by env-var name only."""

    def prepare(self, ctx: RunContext, tmp: Path) -> dict[str, str]:
        return {}

    def child_env_names(self) -> list[str]:
        names = super().child_env_names()  # type: ignore[misc]
        prov = self.capabilities.get("provider", DEFAULT_PROVIDER)  # type: ignore[attr-defined]
        if prov:
            names += [n for n in (prov.get("api_key_env"), prov.get("base_url_env")) if n]
        return list(dict.fromkeys(names))

    async def run(self, ctx: RunContext, runner) -> AdapterResult:
        with tempfile.TemporaryDirectory(prefix=f"orch-{self.kind}-") as tmp:  # type: ignore[attr-defined]
            return await runner(
                argv=self.build_command(ctx),  # type: ignore[attr-defined]
                cwd=ctx.cwd,
                env_names=self.child_env_names(),  # type: ignore[attr-defined]
                artifacts_dir=ctx.artifacts_dir,
                run_id=ctx.run_id,
                timeout_seconds=ctx.timeout_seconds,
                stdin_data=self.stdin_bytes(ctx),  # type: ignore[attr-defined]
                extra_env=self.prepare(ctx, Path(tmp)),
            )
