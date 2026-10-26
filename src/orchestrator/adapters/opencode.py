from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter
from orchestrator.adapters.common import JsonOutputMixin


class OpenCodeAdapter(JsonOutputMixin, BaseHarnessAdapter):
    """OpenCode (opencode.ai). Non-interactive mode is `opencode run "<message>"`;
    `--format json` prints events, including per-step token usage.

    Experimental: checked against OpenCode 1.19.4 on 2026-10-26 with a local mock API.
    Model selection uses `-m provider/model`. The process cwd is the worktree, so no --cwd
    flag is passed.
    """

    kind = "opencode"
    experimental = True
    default_command = "opencode"
    default_args = ["run", "--format", "json", "?--model={model}", "{prompt}"]
    default_env_vars = [
        "OPENCODE_CONFIG",
        "ANTHROPIC_API_KEY",
        "QWEN_API_KEY",
        "DEEPSEEK_API_KEY",
        "GLM_API_KEY",
    ]
    description = "General, model-agnostic coding harness (preferred default)."
    verify_hint = "opencode --version && opencode run --help"
