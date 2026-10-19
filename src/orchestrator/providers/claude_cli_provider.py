"""Claude Code CLI provider: run Architect/planning calls through `claude -p` so they use
the operator's own Claude subscription (Pro/Max) instead of an API key.

Policy basis (checked 2026-10-19): Anthropic's Help Center says subscribers "can still use
... `claude -p` ... with your subscription limits", and its legal page reserves OAuth
sign-in for "ordinary use of Claude Code and other native Anthropic applications". So this
provider only ever runs the *unmodified* `claude` binary signed in with your account; it
never reads or forwards the OAuth token itself.

Safety/role rules:
  - the Architect has NO execution access: all built-in tools are removed (`--tools ""`)
    and MCP tools are denied (`--disallowedTools mcp__*`, `--strict-mcp-config` with no
    config), so the model can only read the prompt and answer;
  - ANTHROPIC_API_KEY is deliberately NOT passed to the child, otherwise Claude Code would
    bill the API key instead of the subscription;
  - calls are serialised (one at a time) to stay within "ordinary, individual usage";
  - the static prompt prefix goes in `--system-prompt-file` (cached by Claude Code), the
    dynamic state goes on stdin.

Flags verified against https://code.claude.com/docs/en/cli-reference on 2026-10-19.
Extra flags (e.g. `--safe-mode` to skip your user CLAUDE.md/plugins) can be appended via
CLAUDE_CLI_EXTRA_ARGS.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.providers.base import (
    BaseProvider,
    CompletionRequest,
    CompletionResult,
    ProviderError,
)

# Env vars passed to `claude` (besides the base allowlist). Never ANTHROPIC_API_KEY.
CLAUDE_ENV_VARS = ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"]
_LOCK = asyncio.Lock()


def claude_command() -> str:
    return os.environ.get("CLAUDE_CLI_COMMAND") or "claude"


def extra_args() -> list[str]:
    return shlex.split(os.environ.get("CLAUDE_CLI_EXTRA_ARGS", ""))


class ClaudeCliProvider(BaseProvider):
    kind = "claude_cli"

    def validate_config(self) -> list[str]:
        if shutil.which(claude_command()) is None:
            return [
                f"`{claude_command()}` not found on PATH (install Claude Code and run "
                "`claude auth login`)"
            ]
        return []

    def build_argv(self, req: CompletionRequest, system_prompt_file: Path) -> list[str]:
        argv = [
            claude_command(),
            "-p",
            "--output-format",
            "json",
            "--model",
            req.model,
            "--system-prompt-file",
            str(system_prompt_file),
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if req.effort:
            argv += ["--effort", req.effort]
        return argv + extra_args()

    async def health(self) -> dict[str, Any]:
        problems = self.validate_config()
        if problems:
            return {"ok": False, "problems": problems, "network_checked": False}
        from orchestrator.services.process_service import run_capture

        code, out, err, _ = await run_capture(
            [claude_command(), "auth", "status"], Path.home(), CLAUDE_ENV_VARS, None, 20
        )
        method = None
        try:
            method = json.loads(out).get("authMethod")
        except (ValueError, AttributeError):
            pass
        notes = []
        if code != 0:
            problems.append("not signed in: run `claude auth login` (Claude subscription)")
        if method in ("api_key", "api_key_helper"):
            notes.append("Claude Code is using an API key, not your subscription")
        return {
            "ok": not problems,
            "problems": problems,
            "auth_method": method,
            "notes": notes,
            "network_checked": False,
        }

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        from orchestrator.services.process_service import run_capture

        problems = self.validate_config()
        if problems:
            raise ProviderError("; ".join(problems))
        with tempfile.TemporaryDirectory(prefix="orch-claude-") as tmp:
            scratch = Path(tmp)
            sys_file = scratch / "system-prompt.md"
            sys_file.write_text(req.prompt.static_text(), encoding="utf-8")
            argv = self.build_argv(req, sys_file)
            async with _LOCK:
                # Empty scratch cwd: no project CLAUDE.md, settings, or files to read.
                code, out, err, timed_out = await run_capture(
                    argv,
                    scratch,
                    CLAUDE_ENV_VARS,
                    req.prompt.dynamic_text().encode(),
                    req.timeout_seconds,
                )
        if timed_out:
            raise ProviderError(f"`claude -p` timed out after {req.timeout_seconds}s")
        return parse_output(code, out, err, req.model)


def parse_output(code: int | None, out: str, err: str, model: str) -> CompletionResult:
    """Parse `claude -p --output-format json`. Fails clearly on errors/limits."""
    from orchestrator.utils.redaction import redact

    try:
        data = json.loads(out)
    except ValueError:
        tail = redact((err or out).strip())[-500:]
        raise ProviderError(f"`claude -p` exited {code} without JSON output: {tail}") from None
    if not isinstance(data, dict):
        raise ProviderError("unexpected `claude -p` output shape")
    text = str(data.get("result") or "")
    if data.get("is_error") or code not in (0, None):
        raise ProviderError(
            f"`claude -p` reported an error ({data.get('subtype')}): {redact(text)[:500]}"
        )
    u = data.get("usage") or {}
    usage = {
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
        # Client-side estimate only; on a subscription this is not billed.
        "estimated_cost_usd": data.get("total_cost_usd"),
        "via": "claude_cli_subscription",
    }
    return CompletionResult(text=text, model=model, usage=usage, stop_reason=data.get("subtype"))
