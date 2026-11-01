"""Which harnesses can run interactively, and the controls each one offers before a
session starts. Drivers replace these lists with what the harness itself reports."""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.adapters.chat.acp import AcpDriver
from orchestrator.adapters.chat.base import ChatDriver
from orchestrator.adapters.chat.claude_stream import ClaudeDriver
from orchestrator.adapters.chat.mock import MockDriver
from orchestrator.adapters.chat.pi_rpc import PiDriver


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    description: str
    dangerous: bool = False


@dataclass(frozen=True)
class HarnessProfile:
    driver: type[ChatDriver]
    permissions: list[Mode] = field(default_factory=list)
    default_permission: str | None = None
    efforts: list[str] = field(default_factory=list)
    default_effort: str | None = None
    # False when the harness names models its own way, so the list arrives once it starts.
    uses_profile_models: bool = True
    note: str | None = None
    # Starting permission mode for standalone runs, by access level.
    unattended: dict[str, str] = field(default_factory=dict)


YOLO = "Runs every tool without asking."
HARNESSES: dict[str, HarnessProfile] = {
    "pi": HarnessProfile(
        PiDriver,
        [
            Mode("plan", "Plan", "Read-only: blocks edits and non-read shell commands."),
            Mode("ask", "Ask first", "Asks before edits and shell commands."),
            Mode("auto-edit", "Auto-edit", "Edits freely, asks before shell commands."),
            Mode("yolo", "Yolo", YOLO, dangerous=True),
        ],
        "ask",
        ["off", "minimal", "low", "medium", "high"],
        "medium",
        note="Pi has no permission system; the orchestrator adds prompts, not a sandbox.",
        unattended={"read": "report", "write": "ask"},
    ),
    "qwen_code": HarnessProfile(
        AcpDriver,
        [
            Mode("plan", "Plan", "Analyze only; no edits or commands."),
            Mode("default", "Ask first", "Asks before edits and shell commands."),
            Mode("auto-edit", "Auto-edit", "Approves edits automatically."),
            Mode("auto", "Auto", "A model approves safe actions and blocks risky ones."),
            Mode("yolo", "Yolo", YOLO, dangerous=True),
        ],
        "default",
        unattended={"read": "default", "write": "default"},
    ),
    "opencode": HarnessProfile(
        AcpDriver,
        [
            Mode("build", "Ask first", "Asks before edits, shell commands and web fetches."),
            Mode("plan", "Plan", "Disallows edit tools."),
        ],
        "build",
        uses_profile_models=False,
        unattended={"read": "build", "write": "build"},
    ),
    "deepseek_harness": HarnessProfile(
        AcpDriver,
        [
            Mode("read-only", "Read-only", "Its sandbox blocks writes; going further asks you."),
            Mode("workspace-write", "Workspace", "Writes in the workspace without asking."),
            Mode("danger-full-access", "Yolo", YOLO, dangerous=True),
        ],
        "read-only",
        ["off", "low", "high", "max"],
        "high",
        note="Sandbox mode is fixed at start. Workspace mode writes without asking.",
        unattended={"read": "read-only", "write": "workspace-write"},
    ),
    "claude_cli": HarnessProfile(
        ClaudeDriver,
        [
            Mode("plan", "Plan", "Read-only until you approve a plan."),
            Mode("manual", "Ask first", "Asks before edits and shell commands."),
            Mode("acceptEdits", "Auto-edit", "Edits freely, asks before shell commands."),
            Mode("auto", "Auto", "Claude Code's classifier approves safe actions."),
            Mode("bypassPermissions", "Yolo", YOLO, dangerous=True),
        ],
        "manual",
        ["low", "medium", "high", "xhigh", "max"],
        "medium",
        unattended={"read": "manual", "write": "manual"},
    ),
    "mock": HarnessProfile(
        MockDriver,
        [
            Mode("ask", "Ask first", "Asks before writing."),
            Mode("yolo", "Yolo", YOLO, dangerous=True),
        ],
        "ask",
        ["low", "high"],
        "low",
        unattended={"read": "ask", "write": "ask"},
    ),
}

UNSUPPORTED = {
    "zcode": "ZCode's app-server is a private protocol; not supported in Chat yet.",
    "aider": "Aider has no machine-readable session mode.",
}
