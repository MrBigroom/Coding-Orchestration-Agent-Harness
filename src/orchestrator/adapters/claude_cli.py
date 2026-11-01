from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter
from orchestrator.adapters.common import JsonOutputMixin


class ClaudeCliAdapter(JsonOutputMixin, BaseHarnessAdapter):
    """Claude Code CLI (`claude -p "<prompt>"`, print/headless mode).

    Experimental and disabled by default. Runs the unmodified `claude` binary signed in
    with your own Claude subscription (Anthropic allows `claude -p` on subscription
    limits). ANTHROPIC_API_KEY is deliberately not passed through, so runs never silently
    switch to API billing; `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is passed
    if set. `--permission-mode acceptEdits` lets it edit files without prompting, and
    `--allowedTools Bash` auto-approves shell commands inside the isolated worktree;
    without it the worker can't run tests or commit in `-p` mode. `--output-format json`
    adds token usage (including cache reads) to the result.
    """

    kind = "claude_cli"
    experimental = True
    default_command = "claude"
    default_args = [
        "-p",
        "{prompt}",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "?--model={model}",
        "--allowedTools",
        "Bash",
    ]
    default_env_vars = ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"]
    description = "Optional Claude Code headless runs (reserve for escalations)."
    verify_hint = "claude --version && claude -p 'say hi'"
