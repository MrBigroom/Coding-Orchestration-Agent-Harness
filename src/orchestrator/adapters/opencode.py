from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter


class OpenCodeAdapter(BaseHarnessAdapter):
    """OpenCode (opencode.ai). Non-interactive mode is `opencode run "<message>"`.

    Experimental: flags were checked against public docs on 2026-10-19 but not exercised
    by this project's tests. Verify with `opencode run --help`. Model selection uses
    `-m provider/model`. The process cwd is the worktree, so no --cwd flag is passed.
    """

    kind = "opencode"
    experimental = True
    default_command = "opencode"
    default_args = ["run", "?--model={model}", "{prompt}"]
    default_env_vars = [
        "OPENCODE_CONFIG",
        "ANTHROPIC_API_KEY",
        "QWEN_API_KEY",
        "DEEPSEEK_API_KEY",
        "GLM_API_KEY",
    ]
    description = "General, model-agnostic coding harness (preferred default)."
    verify_hint = "opencode --version && opencode run --help"
