"""`claude -p` Architect provider, exercised against a fake `claude` binary (no network)."""

import asyncio
import json
import stat
import sys

import pytest

from orchestrator.providers.base import (
    BuiltPrompt,
    CompletionRequest,
    PromptSection,
    ProviderConfig,
    ProviderError,
)
from orchestrator.providers.claude_cli_provider import ClaudeCliProvider, parse_output

FAKE = """#!{python}
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read()
sp = open(args[args.index("--system-prompt-file") + 1]).read()
record = {{"args": args, "stdin": stdin, "system": sp,
          "saw_api_key": "ANTHROPIC_API_KEY" in os.environ, "cwd_files": os.listdir(".")}}
open(os.environ["FAKE_RECORD"], "w").write(json.dumps(record))
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "ok":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
        "result": "VERDICT: APPROVED\\nFINDINGS:\\n- none",
        "usage": {{"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7}},
        "total_cost_usd": 0.01}}))
elif mode == "limit":
    print(json.dumps({{"type": "result", "subtype": "error", "is_error": True,
                      "result": "usage limit reached"}}))
    sys.exit(1)
else:
    print("garbage", file=sys.stderr)
    sys.exit(2)
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.json"
    monkeypatch.setenv("CLAUDE_CLI_COMMAND", str(path))
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-should-never-reach-claude")
    # FAKE_* must reach the child for the test; production passes only CLAUDE_* names.
    monkeypatch.setattr(
        "orchestrator.providers.claude_cli_provider.CLAUDE_ENV_VARS",
        ["CLAUDE_CODE_OAUTH_TOKEN", "FAKE_RECORD", "FAKE_MODE"],
    )
    return record


def _req():
    prompt = BuiltPrompt(
        role="architect",
        sections=[
            PromptSection("system", "You are the Architect", static=True, cache_breakpoint=True),
            PromptSection("dynamic_state", "the diff", static=False),
        ],
    )
    return CompletionRequest(
        prompt=prompt, model="claude-opus-5-5", effort="high", timeout_seconds=30
    )


def test_runs_claude_print_mode_without_tools_or_api_key(fake_claude):
    provider = ClaudeCliProvider(ProviderConfig(name="c", kind="claude_cli"))
    res = asyncio.run(provider.complete(_req()))
    assert res.text.startswith("VERDICT: APPROVED")
    assert res.usage["via"] == "claude_cli_subscription"
    assert res.usage["cache_read_input_tokens"] == 7
    rec = json.loads(fake_claude.read_text())
    args = rec["args"]
    assert args[0] == "-p" and args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--tools") + 1] == ""  # no built-in tools
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--effort") + 1] == "high"
    assert rec["saw_api_key"] is False  # subscription billing only
    assert "You are the Architect" in rec["system"]  # static prefix -> system prompt
    assert "the diff" in rec["stdin"] and "the diff" not in rec["system"]
    assert set(rec["cwd_files"]) == {"system-prompt.md"}  # empty scratch dir


@pytest.mark.parametrize("mode,match", [("limit", "usage limit"), ("crash", "without JSON")])
def test_errors_are_provider_errors(fake_claude, monkeypatch, mode, match):
    monkeypatch.setenv("FAKE_MODE", mode)
    provider = ClaudeCliProvider(ProviderConfig(name="c", kind="claude_cli"))
    with pytest.raises(ProviderError, match=match):
        asyncio.run(provider.complete(_req()))


def test_missing_binary_reported(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_COMMAND", "/nonexistent/claude")
    assert ClaudeCliProvider(ProviderConfig(name="c", kind="claude_cli")).validate_config()


def test_parse_output_rejects_non_object():
    with pytest.raises(ProviderError):
        parse_output(0, "[]", "", "m")


def test_architect_routes_to_claude_cli_profile(session):
    from orchestrator.services import provider_service

    choice = provider_service.choose_profile(session, None, "architect")
    assert choice.profile.name == "architect-claude-cli"
    assert choice.profile.model_name == "claude-opus-5-5"


def test_review_through_claude_cli_end_to_end(session, fake_claude):
    """Architect review via the CLI provider produces a parsed review."""
    from orchestrator.services.review_service import parse_review

    provider = ClaudeCliProvider(ProviderConfig(name="c", kind="claude_cli"))
    res = asyncio.run(provider.complete(_req()))
    result, ambiguous = parse_review(res.text)
    assert result.verdict == "APPROVED" and not ambiguous


def test_worker_adapter_never_forwards_api_key():
    from orchestrator.adapters.claude_cli import ClaudeCliAdapter

    assert "ANTHROPIC_API_KEY" not in ClaudeCliAdapter.default_env_vars
