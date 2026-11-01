"""Usage/final-text parsing for harness JSON output, and per-run config generation.

The output samples are trimmed from real runs of each CLI against a local mock API that
reported 1000 prompt tokens (600 cached) and 7 output tokens (scripts/harness-bench).
"""

import json
from pathlib import Path

from orchestrator.adapters.base import RunContext
from orchestrator.adapters.common import final_text, parse_usage
from orchestrator.adapters.registry import build_adapter, default_specs
from orchestrator.services.usage_service import weighted_tokens

EXPECTED = {
    "input_tokens": 1000,
    "cache_read_input_tokens": 600,
    "uncached_input_tokens": 400,
    "output_tokens": 7,
}

QWEN = json.dumps(
    [
        {"type": "system", "subtype": "init", "model": "qwen3.8-flash"},
        {
            "type": "result",
            "subtype": "success",
            "result": "OK",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 7,
                "cache_read_input_tokens": 600,
                "total_tokens": 1007,
            },
            "stats": {"models": {"qwen3.8-flash": {"api": {"totalRequests": 1}}}},
        },
    ]
)
CLAUDE = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "num_turns": 1,
        "result": "OK",
        "usage": {
            "input_tokens": 400,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 600,
            "output_tokens": 7,
        },
    }
)
ZCODE = json.dumps(
    {
        "sessionId": "sess_test",
        "response": "OK",
        "usage": {
            "modelRequestCount": 1,
            "inputTokens": 1000,
            "cacheReadTokens": 600,
            "cacheWriteTokens": 0,
            "outputTokens": 7,
        },
        "projection": {"totalTokenCount": 1007},
    }
).replace('"totalTokenCount": 1007', '"totalTokenCount": [REDACTED]')
_PI_MSG = {
    "role": "assistant",
    "content": [{"type": "text", "text": "OK"}],
    "usage": {"input": 400, "output": 7, "cacheRead": 600, "cacheWrite": 0},
}
PI = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "message_start", "message": {**_PI_MSG, "usage": {"input": 0}}},
        {"type": "message_update", "usage": _PI_MSG["usage"]},
        {"type": "message_end", "message": _PI_MSG},
        {"type": "turn_end", "message": _PI_MSG},
    )
)
DSH = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "session", "id": "s"},
        {"type": "text", "text": "OK"},
        {
            "type": "status",
            "phase": "step_end",
            "turn": 1,
            "step": 1,
            "usage": {
                "inputTokens": 400,
                "outputTokens": 7,
                "totalTokens": 1007,
                "cacheReadTokens": 600,
            },
        },
        {"type": "final", "text": "OK"},
    )
)
OPENCODE = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "text", "part": {"type": "text", "text": "OK"}},
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "tokens": {
                    "total": 1007,
                    "input": 400,
                    "output": 7,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 600},
                },
            },
        },
    )
)


def test_parse_usage_normalises_every_json_harness():
    for kind, out in (
        ("qwen_code", QWEN),
        ("claude_cli", CLAUDE),
        ("pi", PI),
        ("deepseek_harness", DSH),
        ("opencode", OPENCODE),
        ("zcode", ZCODE),
    ):
        usage = parse_usage(kind, out)
        assert usage is not None, kind
        assert {k: usage[k] for k in EXPECTED} == EXPECTED, kind
        assert usage.get("requests", 1) == 1, kind


def test_parse_usage_sums_multi_step_runs_and_ignores_noise():
    two_steps = DSH + "\nnot json\n" + DSH
    assert parse_usage("deepseek_harness", two_steps)["input_tokens"] == 2000
    assert parse_usage("deepseek_harness", two_steps)["requests"] == 2
    assert parse_usage("pi", "garbage") is None
    assert parse_usage("mock", QWEN) is None
    assert parse_usage("qwen_code", "") is None


def test_aider_token_lines():
    out = (
        "Tokens: 2.1k sent, 1.5k cache hit, 300 received.\n"
        "some text\n"
        "Tokens: 846 sent, 12 received. Cost: $0.00 message.\n"
    )
    usage = parse_usage("aider", out)
    assert usage["input_tokens"] == 2946
    assert usage["cache_read_input_tokens"] == 1500
    assert usage["output_tokens"] == 312
    assert usage["requests"] == 2


def test_final_text_from_json_modes():
    assert final_text("qwen_code", QWEN) == "OK"
    assert final_text("claude_cli", CLAUDE) == "OK"
    assert final_text("pi", PI) == "OK"
    assert final_text("deepseek_harness", DSH) == "OK"
    assert final_text("opencode", OPENCODE) == "OK"
    assert final_text("zcode", ZCODE) == "OK"


def test_weighted_tokens_discounts_cache_and_weights_output():
    assert weighted_tokens(EXPECTED) == 400 + 0.2 * 600 + 3 * 7


def _ctx(tmp_path: Path, model: str | None) -> RunContext:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing")
    return RunContext(
        run_id=1,
        task_id=1,
        cwd=tmp_path,
        prompt_file=prompt,
        artifacts_dir=tmp_path,
        handoff_path=tmp_path / "h.json",
        branch="b",
        phase="p",
        model=model,
    )


def _adapter(kind: str):
    return build_adapter(next(s for s in default_specs() if s.kind == kind))


def test_pi_config_references_key_by_name_and_caches_only_supported_models(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-secret-value")
    monkeypatch.delenv("QWEN_OPENAI_BASE_URL", raising=False)
    pi = _adapter("pi")
    for model, cached in (("qwen3.8-flash", True), ("deepseek-v4.1-flash", False)):
        cfg_dir = tmp_path / model
        cfg_dir.mkdir()
        env = pi.prepare(_ctx(tmp_path, model), cfg_dir)
        raw = (cfg_dir / "models.json").read_text()
        assert "sk-secret-value" not in raw
        prov = json.loads(raw)["providers"]["orchestrator"]
        assert prov["apiKey"] == "$QWEN_API_KEY"
        assert prov["baseUrl"].startswith("https://token-plan.")
        assert ("compat" in prov["models"][0]) is cached
        assert env["PI_CODING_AGENT_DIR"] == str(cfg_dir)
    assert pi.stdin_bytes(_ctx(tmp_path, "m")) == b"do the thing"
    assert "QWEN_API_KEY" in pi.child_env_names()


def test_dsh_patch_routes_model_through_configured_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_OPENAI_BASE_URL", "https://example.test/v1")
    dsh = _adapter("deepseek_harness")
    env = dsh.prepare(_ctx(tmp_path, "deepseek-v4.1-flash"), tmp_path)
    patch = json.loads((tmp_path / "cordis.patch.yml").read_text())
    route = patch[0]["config"]["providers"]["orchestrator"]
    assert route["baseURL"] == "https://example.test/v1"
    assert route["apiKeyEnv"] == "QWEN_API_KEY" and "compat" not in route
    assert patch[1]["config"] == {"provider": "orchestrator", "model": "deepseek-v4.1-flash"}
    assert env == {"DSH_HOME": str(tmp_path)}
    assert dsh.build_command(_ctx(tmp_path, "x")) == ["dsh", "--profile", "headless", "--json", "-"]


def test_qwen_disables_memory_and_maps_token_plan_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-q")
    qwen = _adapter("qwen_code")
    env = qwen.prepare(_ctx(tmp_path, "qwen3.8-max"), tmp_path)
    settings = json.loads(Path(env["QWEN_CODE_SYSTEM_SETTINGS_PATH"]).read_text())
    assert settings["memory"]["enableManagedAutoMemory"] is False
    assert env["OPENAI_API_KEY"] == "sk-q" and env["OPENAI_MODEL"] == "qwen3.8-max"
    assert env["HOME"] == str(tmp_path)
    argv = qwen.build_command(_ctx(tmp_path, "qwen3.8-max"))
    assert "run_shell_command" in argv and argv[argv.index("-o") + 1] == "json"


def test_qwen_keeps_own_home_when_provider_is_null(tmp_path):
    spec = next(s for s in default_specs() if s.kind == "qwen_code")
    spec.capabilities["provider"] = None
    assert "HOME" not in build_adapter(spec).prepare(_ctx(tmp_path, "qwen3.8-max"), tmp_path)


def test_claude_template_allows_shell_commands():
    spec = next(s for s in default_specs() if s.kind == "claude_cli")
    assert spec.args_template[-2:] == ["--allowedTools", "Bash"]


def test_zcode_config_uses_token_plan_without_writing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-secret-value")
    monkeypatch.setenv("QWEN_OPENAI_BASE_URL", "https://example.test/v1")
    zcode = _adapter("zcode")
    ctx = _ctx(tmp_path, "glm-5.3")
    env = zcode.prepare(ctx, tmp_path)
    raw = (tmp_path / ".zcode" / "cli" / "config.json").read_text()
    config = json.loads(raw)
    assert "sk-secret-value" not in raw
    assert config["provider"]["qwen"] == {
        "kind": "openai-compatible",
        "options": {"baseURL": "https://example.test/v1"},
        "models": {"glm-5.3": {"id": "glm-5.3"}},
    }
    assert config["model"] == "qwen/glm-5.3"
    assert env == {"HOME": str(tmp_path), "ZCODE_STORAGE_DIR": str(tmp_path / ".zcode")}
    assert "QWEN_API_KEY" in zcode.child_env_names()
    argv = zcode.build_command(ctx)
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[argv.index("--mode") + 1] == "yolo"
    assert argv[-2:] == ["--prompt", "do the thing"]


def test_provider_null_leaves_harness_config_alone(tmp_path):
    spec = next(s for s in default_specs() if s.kind == "deepseek_harness")
    spec.capabilities["provider"] = None
    assert build_adapter(spec).prepare(_ctx(tmp_path, "deepseek-v4.1-flash"), tmp_path) == {}
    assert not (tmp_path / "cordis.patch.yml").exists()
