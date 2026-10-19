from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter


class ClaudeCliAdapter(BaseHarnessAdapter):
    """Claude Code CLI (`claude -p "<prompt>"`, print/headless mode).

    Experimental and disabled by default. Runs the unmodified `claude` binary signed in
    with your own Claude subscription (Anthropic allows `claude -p` on subscription
    limits). ANTHROPIC_API_KEY is deliberately not passed through, so runs never silently
    switch to API billing; `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is passed
    if set. `--permission-mode acceptEdits` lets it edit files without prompting; shell
    commands still need explicit allow rules.
    """

    kind = "claude_cli"
    experimental = True
    default_command = "claude"
    default_args = ["-p", "{prompt}", "--permission-mode", "acceptEdits", "?--model={model}"]
    default_env_vars = ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"]
    description = "Optional Claude Code headless runs (reserve for escalations)."
    verify_hint = "claude --version && claude -p 'say hi'"
