"""Each driver against a stub process that speaks its protocol, so no harness or model runs."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

from orchestrator.adapters.base import AdapterSpec
from orchestrator.adapters.chat.acp import AcpDriver
from orchestrator.adapters.chat.base import (
    ChatDriver,
    DriverError,
    DriverOptions,
    Image,
    text_of,
)
from orchestrator.adapters.chat.claude_stream import ClaudeDriver
from orchestrator.adapters.chat.pi_rpc import PiDriver
from orchestrator.adapters.registry import build_adapter

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


PRELUDE = f"""#!{sys.executable}
import json, sys
def out(o): print(json.dumps(o), flush=True)
"""

PI_STUB = (
    PRELUDE
    + """
out_of_band = None
for line in sys.stdin:
    m = json.loads(line)
    t = m.get("type")
    def ok(data=None): out({"id": m.get("id"), "type": "response", "command": t, "success": True, "data": data})
    if t == "get_state": ok({"model": {"id": "m1", "provider": "x"}, "thinkingLevel": "low"})
    elif t == "get_available_thinking_levels": ok({"levels": ["off", "low"]})
    elif t == "set_thinking_level": ok()
    elif t == "set_model":
        out({"id": m.get("id"), "type": "response", "command": t, "success": False, "error": "no such model"})
    elif t == "prompt":
        ok({"disposition": "queued" if m.get("streamingBehavior") else "started"})
        if m.get("streamingBehavior"): continue
        out({"type": "agent_start"})
        out({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "hm"}})
        out({"type": "extension_ui_request", "id": "ui1", "method": "select", "title": "Allow bash?\\nls -la", "options": ["Allow", "Deny"]})
        answer = json.loads(sys.stdin.readline())
        out({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {"command": "ls"}})
        out({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash", "isError": answer.get("value") != "Allow", "result": {"content": [{"type": "text", "text": answer.get("value")}]}})
        out({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "done\\u2028line"}})
        out({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 10, "cacheRead": 90, "cacheWrite": 0, "output": 5}, "stopReason": "stop"}})
        out({"type": "agent_settled", "aborted": False})
    elif t == "abort": ok()
"""
)

ACP_STUB = (
    PRELUDE
    + """
SID = "s1"
for line in sys.stdin:
    m = json.loads(line)
    method, i, p = m.get("method"), m.get("id"), m.get("params") or {}
    def ok(r): out({"jsonrpc": "2.0", "id": i, "result": r})
    def update(u): out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": SID, "update": u}})
    if method == "initialize": ok({"protocolVersion": 1, "agentCapabilities": {"promptCapabilities": {"image": True}}})
    elif method == "session/new":
        ok({"sessionId": SID,
            "modes": {"currentModeId": "auto", "availableModes": [{"id": "default", "name": "Default"}, {"id": "auto", "name": "Auto"}]},
            "models": {"currentModelId": "route:1", "availableModels": [{"modelId": "route:1", "name": "[Plan] qwen-a"}, {"modelId": "route:2", "name": "[Plan] qwen-b"}]},
            "configOptions": [{"id": "reasoning_effort", "category": "thought_level", "currentValue": "high", "options": [{"group": "g", "options": [{"value": "off", "name": "Off"}, {"value": "high", "name": "High"}]}]}]})
    elif method in ("session/set_mode", "session/set_model", "session/set_config_option"):
        print(json.dumps({"calls": [method, p]}), file=sys.stderr, flush=True)
        ok({})
    elif method == "session/prompt":
        update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hm"}})
        update({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Run ls", "kind": "execute", "status": "pending", "rawInput": {"command": "ls"}})
        out({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {"sessionId": SID, "toolCall": {"toolCallId": "t1", "title": "Run ls", "rawInput": {"command": "ls"}},
             "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"}, {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
        answer = json.loads(sys.stdin.readline())
        out({"jsonrpc": "2.0", "id": 901, "method": "fs/read_text_file", "params": {}})
        refused = json.loads(sys.stdin.readline())
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": answer["result"]["outcome"]["optionId"] + str(refused["error"]["code"])}}]})
        update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
        ok({"stopReason": "end_turn", "usage": {"inputTokens": 100, "cachedReadTokens": 60, "outputTokens": 7}})
"""
)

CLAUDE_STUB = (
    PRELUDE
    + """
out({"type": "system", "subtype": "init", "session_id": "sess-" + str(len(sys.argv)), "model": "opus"})
print(json.dumps(sys.argv), file=sys.stderr, flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if m["type"] == "control_request":
        out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"], "response": {}}})
    elif m["type"] == "user":
        out({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "hi"}}})
        out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}})
        out({"type": "control_request", "request_id": "c1", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}, "permission_suggestions": [{"type": "addRules"}]}})
        answer = json.loads(sys.stdin.readline())
        behavior = answer["response"]["response"]
        out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": behavior["behavior"] == "deny", "content": json.dumps(behavior)}]}})
        out({"type": "result", "usage": {"input_tokens": 5, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 90, "output_tokens": 3}})
"""
)


def stub(tmp_path: Path, source: str) -> str:
    path = tmp_path / "stub-harness"
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def adapter(kind: str, command: str):
    return build_adapter(
        AdapterSpec(
            name=kind,
            kind=kind,
            command=command,
            args_template=[],
            capabilities={"provider": None, "disable_auto_memory": False},
        )
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.changed = asyncio.Event()

    def __call__(self, event: dict) -> None:
        self.events.append(event)
        self.changed.set()

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["type"] == kind]

    async def wait(self, match, timeout: float = 10) -> dict:
        async def poll() -> dict:
            while True:
                for e in self.events:
                    if match(e):
                        return e
                self.changed.clear()
                await self.changed.wait()

        return await asyncio.wait_for(poll(), timeout)

    async def wait_idle(self) -> None:
        await self.wait(lambda e: e == {"type": "status", "status": "idle"})


def options(tmp_path: Path, **kwargs) -> DriverOptions:
    return DriverOptions(cwd=tmp_path, **kwargs)


async def run_turn(driver: ChatDriver, rec: Recorder, choice: str) -> dict:
    await driver.prompt("go")
    approval = await rec.wait(lambda e: e["type"] == "approval")
    await driver.respond(approval["id"], choice)
    await rec.wait_idle()
    return approval


async def test_pi_turn_maps_events_and_gate_prompts(tmp_path):
    rec = Recorder()
    driver = PiDriver(
        adapter("pi", stub(tmp_path, PI_STUB)), options(tmp_path, permission="ask"), rec
    )
    await driver.start()
    assert rec.of("config")[0]["efforts"] == ["off", "low"]
    try:
        approval = await run_turn(driver, rec, "Allow")
        assert (approval["title"], approval["detail"]) == ("Allow bash?", "ls -la")
        assert rec.of("tool_end")[0]["output"] == "Allow"
        # U+2028 is legal inside JSON and must not split a record.
        assert rec.of("text")[0]["delta"] == "done line"
        assert rec.of("usage")[-1]["input_tokens"] == 100
        assert rec.of("usage")[-1]["cache_read_input_tokens"] == 90
        assert rec.of("approval_resolved")[0]["choice"] == "Allow"
    finally:
        await driver.close()


async def test_pi_permission_change_reaches_the_gate_file(tmp_path):
    rec = Recorder()
    driver = PiDriver(
        adapter("pi", stub(tmp_path, PI_STUB)), options(tmp_path, permission="ask"), rec
    )
    await driver.start()
    try:
        mode_file = driver.tmp / "permission-mode"
        assert mode_file.read_text() == "ask"
        await driver.set_permission("plan")
        assert mode_file.read_text() == "plan"
    finally:
        await driver.close()


async def test_pi_refused_command_raises(tmp_path):
    rec = Recorder()
    driver = PiDriver(adapter("pi", stub(tmp_path, PI_STUB)), options(tmp_path), rec)
    await driver.start()
    try:
        with pytest.raises(DriverError, match="no such model"):
            await driver.set_model("missing")
    finally:
        await driver.close()


def test_pi_models_file_lists_every_switchable_model(tmp_path):
    rec = Recorder()
    a = adapter("pi", "pi")
    a.spec.capabilities = {
        "provider": {"base_url_default": "http://x/v1", "api_key_env": "KEY"},
    }
    driver = PiDriver(a, options(tmp_path, model="qwen3.8-flash", models=["glm-5.3"]), rec)
    assert "PI_CODING_AGENT_DIR" in driver.prepared_env()
    driver._write_models()
    config = json.loads((driver.tmp / "models.json").read_text())
    models = config["providers"]["orchestrator"]["models"]
    assert [m["id"] for m in models] == ["qwen3.8-flash", "glm-5.3"]
    assert all(m["reasoning"] for m in models)
    assert models[0]["compat"]["cacheControlFormat"] == "anthropic"
    assert "cacheControlFormat" not in models[1]["compat"]
    assert not any(m["compat"]["supportsDeveloperRole"] for m in models)
    # Pi treats a model as text-only unless told otherwise; glm-5.3 refuses images.
    assert models[0]["input"] == ["text", "image"]
    assert "input" not in models[1]
    assert config["providers"]["orchestrator"]["apiKey"] == "$KEY"
    driver._tmp.cleanup()


async def test_acp_reads_controls_from_the_harness_and_applies_choices(tmp_path):
    rec = Recorder()
    a = adapter("qwen_code", stub(tmp_path, ACP_STUB))
    opts = options(tmp_path, model="qwen-b", effort="off", permission="default")
    driver = AcpDriver(a, opts, rec)
    await driver.start()
    try:
        config = rec.of("config")[-1]
        assert config["model"] == "route:2"  # matched through the display name
        assert config["efforts"] == ["off", "high"]  # flattened from a group
        assert config["effort"] == "off"
        assert config["permission"] == "default"
        assert [m["id"] for m in config["permissions"]] == ["default", "auto"]
        assert config["images"] is True  # from promptCapabilities
    finally:
        await driver.close()


async def test_acp_turn_answers_permissions_and_refuses_unknown_requests(tmp_path):
    rec = Recorder()
    driver = AcpDriver(adapter("qwen_code", stub(tmp_path, ACP_STUB)), options(tmp_path), rec)
    await driver.start()
    try:
        approval = await run_turn(driver, rec, "yes")
        assert approval["title"] == "Run ls"
        assert [o["tone"] for o in approval["options"]] == ["allow", "deny"]
        assert rec.of("tool_end")[0]["output"] == "yes-32601"
        assert rec.of("text")[0]["delta"] == "done"
        usage = rec.of("usage")[-1]
        assert (usage["input_tokens"], usage["cache_read_input_tokens"]) == (100, 60)
    finally:
        await driver.close()


async def test_deepseek_sandbox_mode_is_set_at_start_and_locked(tmp_path):
    rec = Recorder()
    a = adapter("deepseek_harness", stub(tmp_path, ACP_STUB))
    driver = AcpDriver(a, options(tmp_path, permission="read-only"), rec)
    await driver.start()
    try:
        assert driver.process.env["DSH_PERMISSION_MODE"] == "read-only"
        config = rec.of("config")[-1]
        assert config["permission_locked"] and "permissions" not in config
        with pytest.raises(DriverError, match="fixed when the session starts"):
            await driver.set_permission("danger-full-access")
    finally:
        await driver.close()


async def test_opencode_is_told_to_ask(tmp_path):
    rec = Recorder()
    driver = AcpDriver(adapter("opencode", stub(tmp_path, ACP_STUB)), options(tmp_path), rec)
    await driver.start()
    try:
        assert json.loads(driver.process.env["OPENCODE_PERMISSION"])["bash"] == "ask"
    finally:
        await driver.close()


async def test_acp_unknown_model_falls_back_with_a_notice(tmp_path):
    rec = Recorder()
    a = adapter("qwen_code", stub(tmp_path, ACP_STUB))
    driver = AcpDriver(a, options(tmp_path, model="other"), rec)
    await driver.start()
    try:
        assert "isn't offered" in rec.of("notice")[0]["message"]
        assert rec.of("config")[-1]["model"] == "route:1"
    finally:
        await driver.close()


async def test_claude_turn_allows_with_original_input(tmp_path):
    rec = Recorder()
    a = adapter("claude_cli", stub(tmp_path, CLAUDE_STUB))
    driver = ClaudeDriver(a, options(tmp_path, permission="manual", effort="low"), rec)
    await driver.start()
    try:
        approval = await run_turn(driver, rec, "always")
        assert [o["id"] for o in approval["options"]] == ["allow", "always", "deny"]
        sent = json.loads(rec.of("tool_end")[0]["output"])
        assert sent == {
            "behavior": "allow",
            "updatedInput": {"command": "ls"},
            "updatedPermissions": [{"type": "addRules"}],
        }
        usage = rec.of("usage")[-1]
        assert (usage["input_tokens"], usage["cache_read_input_tokens"]) == (100, 90)
    finally:
        await driver.close()


async def test_claude_effort_change_resumes_the_session(tmp_path):
    rec = Recorder()
    a = adapter("claude_cli", stub(tmp_path, CLAUDE_STUB))
    driver = ClaudeDriver(a, options(tmp_path, effort="low"), rec)
    await driver.start()
    try:
        await rec.wait(lambda e: e["type"] == "config" and e.get("model") == "opus")
        session_id = driver._session_id
        await driver.set_effort("high")
        await asyncio.sleep(0.2)
        argv = json.loads(driver.process.stderr_tail[0])
        assert argv[argv.index("--effort") + 1] == "high"
        assert argv[argv.index("--resume") + 1] == session_id
    finally:
        await driver.close()


async def test_acp_usage_handles_input_that_excludes_cached_tokens(tmp_path):
    rec = Recorder()
    driver = AcpDriver(adapter("qwen_code", stub(tmp_path, ACP_STUB)), options(tmp_path), rec)
    driver._on_turn_end({"usage": {"inputTokens": 163, "cachedReadTokens": 1000}})
    usage = rec.of("usage")[-1]
    assert (usage["input_tokens"], usage["uncached_input_tokens"]) == (1163, 163)
    await driver.close()


async def test_exit_is_reported_with_stderr(tmp_path):
    rec = Recorder()
    source = PRELUDE + 'print("boom: bad config", file=sys.stderr, flush=True); sys.exit(3)\n'
    driver = PiDriver(adapter("pi", stub(tmp_path, source)), options(tmp_path), rec)
    with pytest.raises(DriverError):
        await asyncio.wait_for(driver.start(), 5)
    event = await rec.wait(lambda e: e["type"] == "exit")
    assert event["code"] == 3
    assert "boom" in event["message"]
    await driver.close()


async def test_missing_binary_raises(tmp_path):
    rec = Recorder()
    driver = PiDriver(adapter("pi", str(tmp_path / "nope")), options(tmp_path), rec)
    with pytest.raises(DriverError, match="failed to start"):
        await driver.start()
    await driver.close()


def test_text_of_handles_nested_blocks():
    assert text_of([{"type": "content", "content": {"text": "a"}}, {"text": "b"}]) == "a\nb"


class Capture:
    """Stands in for the harness process and keeps what the driver sends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, record: dict) -> None:
        self.sent.append(record)

    async def close(self) -> None:
        return None


async def sent_prompt(driver: ChatDriver, images: list[Image]) -> dict:
    capture = Capture()
    driver.process = capture  # type: ignore[assignment]
    task = asyncio.create_task(driver.prompt("what is this?", images))
    for _ in range(50):
        if capture.sent:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    if turn := getattr(driver, "_turn", None):
        turn.cancel()
    return capture.sent[0]


IMAGE = Image(data="iVBORw0KGgo=", mime="image/png")


async def test_pi_sends_images_with_the_prompt(tmp_path):
    driver = PiDriver(adapter("pi", "pi"), options(tmp_path), Recorder())
    record = await sent_prompt(driver, [IMAGE])
    assert record["message"] == "what is this?"
    assert record["images"] == [{"type": "image", "data": IMAGE.data, "mimeType": "image/png"}]
    driver._tmp.cleanup()


async def test_pi_reports_whether_its_model_takes_images(tmp_path):
    rec = Recorder()
    driver = PiDriver(adapter("pi", stub(tmp_path, PI_STUB)), options(tmp_path), rec)
    await driver.start()
    try:
        assert rec.of("config")[0]["images"] is False  # the stub's model lists no input types
    finally:
        await driver.close()


async def test_acp_sends_image_blocks_before_the_text(tmp_path):
    driver = AcpDriver(adapter("qwen_code", "qwen"), options(tmp_path), Recorder())
    record = await sent_prompt(driver, [IMAGE])
    assert record["params"]["prompt"] == [
        {"type": "image", "data": IMAGE.data, "mimeType": "image/png"},
        {"type": "text", "text": "what is this?"},
    ]
    driver._tmp.cleanup()


async def test_claude_sends_image_blocks_before_the_text(tmp_path):
    driver = ClaudeDriver(adapter("claude_cli", "claude"), options(tmp_path), Recorder())
    record = await sent_prompt(driver, [IMAGE])
    source = {"type": "base64", "media_type": "image/png", "data": IMAGE.data}
    assert record["message"]["content"] == [
        {"type": "image", "source": source},
        {"type": "text", "text": "what is this?"},
    ]
    driver._tmp.cleanup()
