from __future__ import annotations

import asyncio
import itertools
import json
import os
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.chat.base import (
    RESPONSE_TIMEOUT,
    ChatDriver,
    DriverError,
    Image,
    slash_commands,
    text_of,
)
from orchestrator.adapters.common import _n, takes_images, wants_explicit_cache

GATE = Path(__file__).with_name("pi_gate.ts")
PROVIDER = "orchestrator"
# Your Pi MCP servers, their sign-ins and your extensions, which a managed config folder
# would otherwise hide.
OWN_FILES = ("mcp.json", "mcp-auth.json", "extensions")
FAILED_STOP_REASONS = {"length", "error"}
# Pi refuses a plain prompt while a run is active; the same message, sent as a steer, queues.
ALREADY_RUNNING = "already processing"
EDIT_TITLES = {"Allow edit?", "Allow write?"}


class PiDriver(ChatDriver):
    """Pi's RPC mode (`pi --mode rpc`), checked against Pi 1.4.0.

    Pi has no approval step of its own, so `pi_gate.ts` adds one through Pi's extension UI
    requests. `--no-approve` keeps project-local Pi extensions, which could override the
    gate, from loading. Models come from a managed provider, as in batch runs; for chat its
    `models.json` lists every model the session may switch to and marks them as reasoning
    models, so that the effort control reaches them, and those that take images as such.
    Pi's own report of the current model decides whether the GUI offers images.

    Your MCP servers (`~/.pi/agent/mcp.json`) and their sign-ins are linked into that
    folder, so a token Pi refreshes is saved back to your file. Each MCP use then passes
    the gate like any other tool call. Your extensions (`~/.pi/agent/extensions`) are linked
    the same way; their tools pass the gate too. Batch runs have no gate, so they get none
    of these.
    """

    kind = "pi"
    can_steer = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._ids = itertools.count(1)
        self._mode_file = self.tmp / "permission-mode"
        self._busy = False
        self._compacting = False
        self._managed = False
        self._session_id = ""

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
            # Without it Pi assumes 128K and compacts at about 112K, whatever the model holds.
            if window := self.options.context_windows.get(name):
                entry["contextWindow"] = window
            if takes_images(name, self.adapter.capabilities):
                entry["input"] = ["text", "image"]
            entries.append(entry)
        config["providers"][PROVIDER]["models"] = entries
        path.write_text(json.dumps(config, indent=2))

    def _link_own(self) -> None:
        own = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
        for name in OWN_FILES:
            if (own / name).exists():
                (self.tmp / name).symlink_to(own / name)

    async def start(self) -> None:
        env = self.prepared_env()
        self._managed = "PI_CODING_AGENT_DIR" in env
        if self._managed:
            self._write_models()
            self._link_own()
        self._mode_file.write_text(self.options.permission or "ask")
        env["ORCHESTRATOR_PI_MODE_FILE"] = str(self._mode_file)
        if self.options.output_dir:
            env["ORCHESTRATOR_OUTPUT_DIR"] = str(self.options.output_dir)
        argv = [self.adapter.spec.command, "--mode", "rpc", "--no-approve"]
        if self.options.state_dir:
            # Kept in the session's folder, so Continue can reload it and Delete removes it.
            self._session_id = self.options.resume or str(uuid.uuid4())
            argv += ["--session-dir", str(self.options.state_dir), "--session-id", self._session_id]
        else:
            argv += ["--no-session"]
        argv += ["--extension", str(GATE)]
        if self._managed:
            argv += ["--provider", PROVIDER]
        if self.options.model:
            argv += ["--model", self.options.model]
        if self.options.effort:
            argv += ["--thinking", self.options.effort]
        await self.spawn(argv, env)
        if self.options.resume:
            # A missing session only gets a warning and starts empty, so check it loaded.
            state = await self._request("get_state")
            if not state.get("messageCount"):
                raise DriverError("Pi has no saved conversation to continue")
        await self._publish_config()
        await self._publish_commands()
        if self._session_id:
            self.emit({"type": "config", "harness_session": self._session_id})

    async def _request(self, type_: str, **fields: Any) -> Any:
        assert self.process
        request_id = str(next(self._ids))
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            await self.process.send({"id": request_id, "type": type_, **fields})
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(future), RESPONSE_TIMEOUT)
                except TimeoutError as e:
                    # Pi answers a prompt only after compacting, which can take minutes.
                    if self._compacting:
                        continue
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

    async def _publish_commands(self) -> None:
        """Skills, prompt templates and extension commands, which Pi runs from a prompt.
        Its built-in extension commands (`/mcp`, `/llama`) manage Pi through dialogs that
        Chat can't show, so they're left out."""
        try:
            listed = await self._request("get_commands")
        except DriverError:
            return  # a Pi without get_commands still runs commands typed in full
        commands = [
            c
            for c in listed.get("commands") or []
            if (c.get("sourceInfo") or {}).get("source") != "builtin"
        ]
        self.emit({"type": "config", "commands": slash_commands(commands)})

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
        except DriverError as e:
            if ALREADY_RUNNING not in str(e) or "streamingBehavior" in fields:
                self._set_idle()
                raise
            # A run we lost track of is still going, so queue the message on it instead.
            result = await self._request("prompt", **fields, streamingBehavior="steer")
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
        elif kind == "compaction_start":
            self._compacting = True
            self.emit({"type": "notice", "message": "Compacting the conversation…"})
        elif kind == "compaction_end":
            self._compacting = False
            self._on_compaction_end(record)
        elif kind == "thinking_level_changed":
            self.emit({"type": "config", "effort": record.get("level")})
        elif kind == "extension_ui_request":
            await self._on_ui_request(record)

    def _on_message_end(self, message: dict[str, Any]) -> None:
        if message.get("role") != "assistant":
            return
        usage = message.get("usage")
        if isinstance(usage, dict):
            inp = _n(usage.get("input")) + _n(usage.get("cacheWrite"))
            cached = _n(usage.get("cacheRead"))
            # One assistant message is one request, so its input is the context size.
            self.add_usage(
                inp, cached, _n(usage.get("output")), total_input=False, context=inp + cached
            )
        if message.get("stopReason") in FAILED_STOP_REASONS:
            reason = message.get("errorMessage") or f"stopped: {message['stopReason']}"
            self.emit({"type": "error", "message": f"Pi: {reason}"})

    def _on_compaction_end(self, record: dict[str, Any]) -> None:
        result = record.get("result")
        if isinstance(result, dict):
            before, after = _n(result.get("tokensBefore")), _n(result.get("estimatedTokensAfter"))
            message = "Compacted the conversation"
            if before and after:
                message += f" from {before:,} to about {after:,} tokens"
            self.emit({"type": "notice", "message": message + "."})
            if after:
                self.set_context(after)
        elif record.get("aborted"):
            self.emit({"type": "notice", "message": "Compaction was stopped."})
        else:
            reason = record.get("errorMessage") or "no reason given"
            message = f"Pi couldn't compact the conversation: {reason}"
            self.emit({"type": "error", "message": message})

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
