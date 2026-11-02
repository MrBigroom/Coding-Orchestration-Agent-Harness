from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

from orchestrator.adapters.chat.base import (
    RESPONSE_TIMEOUT,
    ChatDriver,
    DriverError,
    Image,
    text_of,
)
from orchestrator.adapters.common import _n, takes_images, wants_explicit_cache

GATE = Path(__file__).with_name("pi_gate.ts")
PROVIDER = "orchestrator"
FAILED_STOP_REASONS = {"length", "error"}
EDIT_TITLES = {"Allow edit?", "Allow write?"}


class PiDriver(ChatDriver):
    """Pi's RPC mode (`pi --mode rpc`), checked against Pi 1.4.0.

    Pi has no approval step of its own, so `pi_gate.ts` adds one through Pi's extension UI
    requests. `--no-approve` keeps project-local Pi extensions, which could override the
    gate, from loading. Models come from a managed provider, as in batch runs; for chat its
    `models.json` lists every model the session may switch to and marks them as reasoning
    models, so that the effort control reaches them, and those that take images as such.
    Pi's own report of the current model decides whether the GUI offers images.
    """

    kind = "pi"
    can_steer = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._ids = itertools.count(1)
        self._mode_file = self.tmp / "permission-mode"
        self._busy = False
        self._managed = False

    def _write_models(self) -> None:
        path = self.tmp / "models.json"
        config = json.loads(path.read_text())
        models = list(dict.fromkeys([m for m in [self.options.model, *self.options.models] if m]))
        entries = []
        for name in models:
            # Reasoning models default to the `developer` role, which Model Studio rejects.
            compat: dict[str, Any] = {"supportsDeveloperRole": False, "thinkingFormat": "qwen"}
            if wants_explicit_cache(name, self.adapter.capabilities):
                compat["cacheControlFormat"] = "anthropic"
            entry = {"id": name, "name": name, "reasoning": True, "compat": compat}
            if takes_images(name, self.adapter.capabilities):
                entry["input"] = ["text", "image"]
            entries.append(entry)
        config["providers"][PROVIDER]["models"] = entries
        path.write_text(json.dumps(config, indent=2))

    async def start(self) -> None:
        env = self.prepared_env()
        self._managed = "PI_CODING_AGENT_DIR" in env
        if self._managed:
            self._write_models()
        self._mode_file.write_text(self.options.permission or "ask")
        env["ORCHESTRATOR_PI_MODE_FILE"] = str(self._mode_file)
        if self.options.output_dir:
            env["ORCHESTRATOR_OUTPUT_DIR"] = str(self.options.output_dir)
        argv = [self.adapter.spec.command, "--mode", "rpc", "--no-session", "--no-approve"]
        argv += ["--extension", str(GATE)]
        if self._managed:
            argv += ["--provider", PROVIDER]
        if self.options.model:
            argv += ["--model", self.options.model]
        if self.options.effort:
            argv += ["--thinking", self.options.effort]
        await self.spawn(argv, env)
        await self._publish_config()

    async def _request(self, type_: str, **fields: Any) -> Any:
        assert self.process
        request_id = str(next(self._ids))
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            await self.process.send({"id": request_id, "type": type_, **fields})
            return await asyncio.wait_for(future, RESPONSE_TIMEOUT)
        except TimeoutError as e:
            tail = " | ".join(self.process.stderr_tail)
            raise DriverError(f"Pi did not answer {type_}: {tail[-500:]}") from e
        finally:
            self._waiting.pop(request_id, None)

    async def _publish_config(self) -> None:
        state = await self._request("get_state")
        levels = await self._request("get_available_thinking_levels")
        model = state.get("model") or {}
        self.images = "image" in (model.get("input") or [])
        self.emit(
            {
                "type": "config",
                "model": model.get("id") or self.options.model,
                "effort": state.get("thinkingLevel"),
                "efforts": levels.get("levels") or [],
                "images": self.images,
            }
        )

    async def prompt(self, text: str, images: list[Image] | None = None) -> None:
        fields: dict[str, Any] = {"message": text}
        if images:
            fields["images"] = [
                {"type": "image", "data": i.data, "mimeType": i.mime} for i in images
            ]
        if self._busy:
            fields["streamingBehavior"] = "steer"
        else:
            self._busy = True
            self.emit({"type": "status", "status": "busy"})
        try:
            result = await self._request("prompt", **fields)
        except DriverError:
            self._set_idle()
            raise
        if isinstance(result, dict) and result.get("disposition") == "handled":
            self._set_idle()

    def _set_idle(self) -> None:
        self._busy = False
        self.emit({"type": "status", "status": "idle"})

    async def interrupt(self) -> None:
        for approval_id in list(self.approvals):
            await self._answer(approval_id, {"cancelled": True}, None)
        await self._request("abort")

    async def _answer(self, approval_id: str, body: dict, choice: str | None) -> None:
        assert self.process
        await self.process.send({"type": "extension_ui_response", "id": approval_id, **body})
        self.resolved(approval_id, choice)

    async def respond(self, approval_id: str, choice: str) -> None:
        if approval_id not in self.approvals:
            raise DriverError("that approval is no longer pending")
        await self._answer(approval_id, {"value": choice}, choice)

    async def set_model(self, model: str) -> None:
        provider = (
            PROVIDER if self._managed else (await self._request("get_state"))["model"]["provider"]
        )
        await self._request("set_model", provider=provider, modelId=model)
        await self._publish_config()

    async def set_effort(self, effort: str) -> None:
        await self._request("set_thinking_level", level=effort)
        self.emit({"type": "config", "effort": effort})

    async def set_permission(self, permission: str) -> None:
        self._mode_file.write_text(permission)
        self.emit({"type": "config", "permission": permission})

    async def on_record(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "response":
            future = self._waiting.get(record.get("id") or "")
            if future and not future.done():
                if record.get("success"):
                    future.set_result(record.get("data") or {})
                else:
                    future.set_exception(
                        DriverError(record.get("error") or "Pi refused the command")
                    )
        elif kind == "message_update":
            update = record.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta":
                self.emit({"type": "text", "delta": update.get("delta", "")})
            elif update.get("type") == "thinking_delta":
                self.emit({"type": "thinking", "delta": update.get("delta", "")})
        elif kind == "message_end":
            self._on_message_end(record.get("message") or {})
        elif kind == "tool_execution_start":
            self.emit(
                {
                    "type": "tool_start",
                    "id": record.get("toolCallId"),
                    "name": record.get("toolName"),
                    "input": record.get("args"),
                }
            )
        elif kind == "tool_execution_end":
            result = record.get("result") or {}
            self.emit(
                {
                    "type": "tool_end",
                    "id": record.get("toolCallId"),
                    "output": text_of(result.get("content")),
                    "error": bool(record.get("isError")),
                }
            )
        elif kind == "agent_settled":
            self._set_idle()
        elif kind == "thinking_level_changed":
            self.emit({"type": "config", "effort": record.get("level")})
        elif kind == "extension_ui_request":
            await self._on_ui_request(record)

    def _on_message_end(self, message: dict[str, Any]) -> None:
        if message.get("role") != "assistant":
            return
        usage = message.get("usage")
        if isinstance(usage, dict):
            self.add_usage(
                _n(usage.get("input")) + _n(usage.get("cacheWrite")),
                _n(usage.get("cacheRead")),
                _n(usage.get("output")),
                total_input=False,
            )
        if message.get("stopReason") in FAILED_STOP_REASONS:
            reason = message.get("errorMessage") or f"stopped: {message['stopReason']}"
            self.emit({"type": "error", "message": f"Pi: {reason}"})

    async def _on_ui_request(self, record: dict[str, Any]) -> None:
        assert self.process
        method = record.get("method")
        if method == "select":
            title, _, detail = str(record.get("title") or "").partition("\n")
            options = [
                {"id": o, "label": o, "tone": "deny" if o == "Deny" else "allow"}
                for o in record.get("options") or []
            ]
            action = {"kind": "write" if title in EDIT_TITLES else "other", "paths": [detail]}
            self.ask(str(record["id"]), title, detail, options, action)
        elif method == "notify":
            self.emit({"type": "notice", "message": str(record.get("message") or "")})
        elif method in ("confirm", "input", "editor"):
            await self.process.send(
                {"type": "extension_ui_response", "id": record["id"], "cancelled": True}
            )
