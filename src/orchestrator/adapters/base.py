"""Harness adapter interface (frozen contract after baseline approval).

Adapters run external coding CLIs headlessly inside an isolated worktree. Command
templates are data (stored in the DB, editable in GUI/CLI); defaults here are only
starting points. Template tokens:

  {cwd} {prompt_file} {prompt} {model} {branch} {artifacts_dir} {run_id} {handoff_path}

Each argv token is formatted independently (no shell). A token prefixed with `?` is
dropped entirely when any placeholder inside it renders empty, e.g. `?--model={model}`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import string
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.core.schemas import Handoff
from orchestrator.utils.json_output import JSONExtractionError, parse_model

MAX_INLINE_PROMPT_BYTES = 100_000  # Linux MAX_ARG_STRLEN is 128 KiB per argument.


@dataclass
class AdapterSpec:
    name: str
    kind: str
    command: str
    args_template: list[str]
    env_vars: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800
    enabled: bool = True
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunContext:
    run_id: int
    task_id: int
    cwd: Path
    prompt_file: Path
    artifacts_dir: Path
    handoff_path: Path
    branch: str
    phase: str
    tags: list[str] = field(default_factory=list)
    model: str | None = None
    timeout_seconds: int = 1800


@dataclass
class AdapterResult:
    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False
    duration_ms: int = 0
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    error: str | None = None


class TemplateError(ValueError):
    pass


class _Formatter(string.Formatter):
    def __init__(self, values: dict[str, str]):
        self.values = values
        self.used_empty = False

    def get_value(self, key, args, kwargs):  # type: ignore[override]
        if isinstance(key, str):
            if key not in self.values:
                raise TemplateError(f"unknown template placeholder {{{key}}}")
            v = self.values[key]
            if v == "":
                self.used_empty = True
            return v
        raise TemplateError("positional placeholders are not allowed")


def render_args(template: list[str], values: dict[str, str]) -> list[str]:
    out: list[str] = []
    for token in template:
        optional = token.startswith("?")
        raw = token[1:] if optional else token
        f = _Formatter(values)
        rendered = f.format(raw)
        if optional and f.used_empty:
            continue
        if rendered == "" and raw.strip().startswith("{") and raw.strip().endswith("}"):
            continue
        out.append(rendered)
    return out


class BaseHarnessAdapter(ABC):  # noqa: B024 - concrete defaults, subclass per harness
    kind: str = "base"
    experimental: bool = True
    default_command: str = ""
    default_args: list[str] = []
    default_env_vars: list[str] = []
    default_capabilities: dict[str, Any] = {}
    description: str = ""
    verify_hint: str = ""

    def __init__(self, spec: AdapterSpec):
        self.spec = spec
        self._proc: asyncio.subprocess.Process | None = None

    # -- discovery ---------------------------------------------------------
    @classmethod
    def default_spec(cls, name: str | None = None) -> AdapterSpec:
        return AdapterSpec(
            name=name or cls.kind,
            kind=cls.kind,
            command=cls.default_command,
            args_template=list(cls.default_args),
            env_vars=list(cls.default_env_vars),
            capabilities={**cls.default_capabilities, "experimental": cls.experimental},
        )

    @property
    def capabilities(self) -> dict[str, Any]:
        return {**self.default_capabilities, **self.spec.capabilities,
                "experimental": self.experimental}

    def resolved_binary(self) -> str | None:
        return shutil.which(self.spec.command)

    def detect(self) -> bool:
        return self.resolved_binary() is not None

    async def health(self) -> dict[str, Any]:
        path = self.resolved_binary()
        return {
            "ok": bool(path) and self.spec.enabled,
            "detected": bool(path),
            "binary": path,
            "enabled": self.spec.enabled,
            "experimental": self.experimental,
            "note": None if path else f"`{self.spec.command}` not found on PATH",
        }

    # -- execution ---------------------------------------------------------
    def template_values(self, ctx: RunContext, include_prompt: bool = True) -> dict[str, str]:
        prompt = ""
        if include_prompt and any("{prompt}" in a for a in self.spec.args_template):
            data = ctx.prompt_file.read_bytes()
            if len(data) > MAX_INLINE_PROMPT_BYTES:
                raise TemplateError(
                    f"prompt is {len(data)} bytes; too large for an inline {{prompt}} argument. "
                    "Use {prompt_file} or stdin mode for this adapter."
                )
            prompt = data.decode()
        return {
            "cwd": str(ctx.cwd),
            "prompt_file": str(ctx.prompt_file),
            "prompt": prompt,
            "model": ctx.model or "",
            "branch": ctx.branch,
            "artifacts_dir": str(ctx.artifacts_dir),
            "run_id": str(ctx.run_id),
            "handoff_path": str(ctx.handoff_path),
        }

    def build_command(self, ctx: RunContext) -> list[str]:
        return [self.spec.command, *render_args(self.spec.args_template,
                                                self.template_values(ctx))]

    def command_preview(self, ctx: RunContext) -> list[str]:
        """Like build_command, but never inlines the prompt text (safe to display/log)."""
        values = self.template_values(ctx, include_prompt=False)
        values["prompt"] = f"<contents of {ctx.prompt_file.name}>"
        return [self.spec.command, *render_args(self.spec.args_template, values)]

    def stdin_bytes(self, ctx: RunContext) -> bytes | None:
        if self.capabilities.get("prompt_via_stdin"):
            return ctx.prompt_file.read_bytes()
        return None

    def child_env_names(self) -> list[str]:
        return list(dict.fromkeys([*self.default_env_vars, *self.spec.env_vars]))

    async def run(self, ctx: RunContext, runner) -> AdapterResult:
        """Run via the process service (`runner` is services.process_service.run_process)."""
        return await runner(
            argv=self.build_command(ctx),
            cwd=ctx.cwd,
            env_names=self.child_env_names(),
            artifacts_dir=ctx.artifacts_dir,
            run_id=ctx.run_id,
            timeout_seconds=ctx.timeout_seconds,
            stdin_data=self.stdin_bytes(ctx),
        )

    async def cancel(self, ctx: RunContext) -> None:
        """Request cancellation; the process service polls for this marker (cross-process)."""
        (ctx.artifacts_dir / "CANCEL").write_text("cancel requested\n")

    def parse_result(self, ctx: RunContext, result: AdapterResult) -> Handoff | None:
        """Prefer the handoff file the worker wrote; fall back to JSON in stdout."""
        for candidate in (ctx.handoff_path, ctx.artifacts_dir / "handoff.json"):
            if candidate.is_file():
                try:
                    return Handoff.model_validate(json.loads(candidate.read_text()))
                except (ValueError, json.JSONDecodeError):
                    continue
        if result.stdout_path and result.stdout_path.is_file():
            tail = result.stdout_path.read_text(errors="replace")[-50_000:]
            try:
                return parse_model(tail, Handoff)
            except JSONExtractionError:
                return None
        return None
