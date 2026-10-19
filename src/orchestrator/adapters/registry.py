"""Adapter kind -> implementation registry and default seed specs."""

from __future__ import annotations

import os

from orchestrator.adapters.aider import AiderAdapter
from orchestrator.adapters.base import AdapterSpec, BaseHarnessAdapter
from orchestrator.adapters.claude_cli import ClaudeCliAdapter
from orchestrator.adapters.deepseek_harness import DeepSeekHarnessAdapter
from orchestrator.adapters.mock_harness import MockHarnessAdapter
from orchestrator.adapters.opencode import OpenCodeAdapter
from orchestrator.adapters.qwen_code import QwenCodeAdapter
from orchestrator.adapters.zcode import ZCodeAdapter

ADAPTER_CLASSES: dict[str, type[BaseHarnessAdapter]] = {
    c.kind: c
    for c in (
        MockHarnessAdapter,
        OpenCodeAdapter,
        DeepSeekHarnessAdapter,
        ZCodeAdapter,
        QwenCodeAdapter,
        AiderAdapter,
        ClaudeCliAdapter,
    )
}

# Env vars that override the default binary name for each adapter.
COMMAND_ENV = {
    "opencode": "OPENCODE_COMMAND",
    "deepseek_harness": "DEEPSEEK_HARNESS_COMMAND",
    "zcode": "ZCODE_COMMAND",
    "qwen_code": "QWEN_CODE_COMMAND",
    "aider": "AIDER_COMMAND",
    "claude_cli": "CLAUDE_CLI_COMMAND",
}

DISABLED_BY_DEFAULT = {"claude_cli"}


def build_adapter(spec: AdapterSpec) -> BaseHarnessAdapter:
    try:
        cls = ADAPTER_CLASSES[spec.kind]
    except KeyError as e:
        raise ValueError(f"unknown adapter kind: {spec.kind}") from e
    return cls(spec)


def default_specs() -> list[AdapterSpec]:
    specs = []
    for kind, cls in ADAPTER_CLASSES.items():
        spec = cls.default_spec()
        env = COMMAND_ENV.get(kind)
        if env and os.environ.get(env):
            spec.command = os.environ[env]
        spec.enabled = kind not in DISABLED_BY_DEFAULT
        specs.append(spec)
    return specs
