from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter


class ClaudeCliAdapter(BaseHarnessAdapter):
    """Claude Code CLI (`claude -p "<prompt>"`, print/headless mode).

    Experimental and disabled by default. Use only with auth the CLI officially supports
    for headless use (e.g. an Anthropic API key); do not assume a consumer subscription
    can be driven as an API. `--permission-mode acceptEdits` lets it edit files without
    prompting; shell commands still need explicit allow rules.
    """

    kind = "claude_cli"
    experimental = True
    default_command = "claude"
    default_args = ["-p", "{prompt}", "--permission-mode", "acceptEdits", "?--model={model}"]
    default_env_vars = ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"]
    description = "Optional Claude Code headless runs (reserve for escalations)."
    verify_hint = "claude --version && claude -p 'say hi'"
