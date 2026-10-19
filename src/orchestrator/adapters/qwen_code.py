from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter


class QwenCodeAdapter(BaseHarnessAdapter):
    """Qwen Code. The binary is `qwen` (not `qwen-code`); headless mode is
    `qwen -p "<prompt>"` and `--approval-mode` controls tool auto-approval.

    Experimental: flags checked against Qwen Code docs on 2026-10-19, not exercised here.
    Default approval mode is `auto-edit` (file edits allowed, shell commands not
    auto-approved). Switch to `--yolo` only if you accept unattended shell execution
    inside the isolated worktree.
    """

    kind = "qwen_code"
    experimental = True
    default_command = "qwen"
    default_args = ["--approval-mode", "auto-edit", "?--model={model}", "-p", "{prompt}"]
    default_env_vars = [
        "QWEN_API_KEY",
        "QWEN_OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
    ]
    description = "Alibaba Qwen harness; preferred for `qwen`-tagged tasks."
    verify_hint = "qwen --version && qwen --help"
