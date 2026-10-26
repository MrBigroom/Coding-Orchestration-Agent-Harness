from __future__ import annotations

import json
import os
from pathlib import Path

from orchestrator.adapters.base import BaseHarnessAdapter, RunContext
from orchestrator.adapters.common import JsonOutputMixin, ManagedConfigMixin, provider_settings


class QwenCodeAdapter(JsonOutputMixin, ManagedConfigMixin, BaseHarnessAdapter):
    """Qwen Code. The binary is `qwen` (not `qwen-code`); headless mode is
    `qwen -p "<prompt>"` and `-o json` prints a result object with token usage.

    Experimental: checked against Qwen Code 0.26.1 on 2026-10-26 with a local mock API.
    - `--approval-mode auto-edit` alone hides the shell tool in `-p` mode, so the worker
      could not run tests or commit. `--allowed-tools run_shell_command` restores it and
      auto-approves shell commands inside the isolated worktree.
    - The `skill` and `notebook_edit` tools are excluded (~1.7k fewer prompt tokens per call).
    - Managed auto-memory is switched off through a per-run system settings file
      (`QWEN_CODE_SYSTEM_SETTINGS_PATH`); otherwise every run makes a second ~6k-token
      "memory extraction" call.
    - The endpoint comes from `capabilities.provider` (default: QWEN_API_KEY +
      QWEN_OPENAI_BASE_URL, i.e. the Token Plan), mapped to Qwen Code's OPENAI_* variables.
      Set `capabilities.provider` to null to use your own ~/.qwen auth instead.
    """

    kind = "qwen_code"
    experimental = True
    default_command = "qwen"
    default_args = [
        "--auth-type",
        "openai",
        "--approval-mode",
        "auto-edit",
        "--allowed-tools",
        "run_shell_command",
        "--exclude-tools",
        "skill",
        "notebook_edit",
        "-o",
        "json",
        "?--model={model}",
        "-p",
        "{prompt}",
    ]
    default_env_vars = [
        "QWEN_API_KEY",
        "QWEN_OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
    ]
    default_capabilities = {"disable_auto_memory": True}
    description = "Alibaba Qwen harness; preferred for `qwen`-tagged tasks."
    verify_hint = "qwen --version && qwen --help"

    def prepare(self, ctx: RunContext, tmp: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.capabilities.get("disable_auto_memory", True):
            settings = tmp / "system-settings.json"
            settings.write_text(
                json.dumps(
                    {"memory": {"enableManagedAutoMemory": False, "enableManagedAutoDream": False}}
                )
            )
            env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] = str(settings)
        prov = provider_settings(self.capabilities)
        key = os.environ.get(prov["api_key_env"]) if prov else None
        if prov and key:
            env["OPENAI_API_KEY"] = key
            env["OPENAI_BASE_URL"] = prov["base_url"]
            if ctx.model:
                env["OPENAI_MODEL"] = ctx.model
        return env
