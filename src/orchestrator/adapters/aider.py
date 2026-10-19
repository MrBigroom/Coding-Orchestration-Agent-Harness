from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter


class AiderAdapter(BaseHarnessAdapter):
    """Aider. `--message-file` runs one instruction non-interactively; `--yes-always`
    auto-confirms. Aider auto-commits by default, which lands on the phase branch.

    Experimental: not exercised by this project's tests. Verify with `aider --help`.
    """

    kind = "aider"
    experimental = True
    default_command = "aider"
    default_args = ["--yes-always", "?--model={model}", "--message-file", "{prompt_file}"]
    default_env_vars = [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "DEEPSEEK_API_KEY",
    ]
    description = "Optional alternative coding harness (fallback after OpenCode)."
    verify_hint = "aider --version"
