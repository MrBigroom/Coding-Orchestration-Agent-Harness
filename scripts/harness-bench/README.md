# Harness overhead bench

Measures how many tokens each coding harness sends **before your task does anything**:
system prompt, tool schemas and injected context. It also counts hidden side calls
(titles, memory extraction) and explicit `cache_control` markers.

```bash
scripts/harness-bench/bench.sh            # all harnesses
scripts/harness-bench/bench.sh pi dsh     # a subset
```

How it works:
- Each CLI is installed into `scripts/harness-bench/.work/` (gitignored).
- Each one runs in a fresh `HOME` with a scrubbed environment, against `mock_server.py`.
  The mock is a local OpenAI chat / OpenAI Responses / Anthropic Messages endpoint that
  logs every request and always answers "OK". No API key or real model is used.
- `count.py` counts tokens per request with the Qwen tokenizer (from `dashscope`).

The mock reports `1000` prompt tokens (`600` cached) and `7` output tokens. The
orchestrator's usage parsers are tested against exactly that output
(`tests/unit/test_harness_usage.py`).

Run it again after harness upgrades. Overhead changes between versions, and so do flags.

## Qoder (opt-in)

```bash
scripts/harness-bench/bench.sh qoder qoder_cache
```

Qoder CLI (`@qoder-ai/qodercli`) won't start without a logged-in Qoder account. It sends
requests to a custom endpoint only when the account's plan allows custom model providers
(BYOK). The CLI checks a server-side `allow_byok` feature switch for this.

So these two targets differ from the rest:
- They run with your **real `HOME`**, so your existing login is used.
  `QODER_PERSONAL_ACCESS_TOKEN` is passed through if it's set.
- The mock is added as a custom provider through `--settings` for that run only, so your
  `~/.qoder` is never edited.
- `qoder_cache` turns on the model's `capabilities.cacheControl`, so you can see whether
  Qoder sends explicit cache markers over the OpenAI protocol.

If a run fails with "Not logged in", run `qodercli` once and `/login`. If it reaches
Qoder but the mock logs no requests, your plan probably doesn't include custom providers.
Not yet checked against a logged-in account: the selection `-m qwen3.8-flash` assumes
custom models are picked by model ID, as `qodercli --help` says.

## Results — 2026-10-26

One trivial prompt, first agent request, Qwen tokens:

| Harness (version) | Tokens | Tools | Side calls |
|---|---|---|---|
| Aider 0.86.2 | ~720 | 0 | – |
| mini-swe-agent 2.4.6 | ~760 | 1 | – |
| Pi 1.4.0 | ~1,400 | 4 | – |
| DeepSeek Harness 0.2.1-rc.2 | ~5,560 | 25 | title ~140 |
| OpenCode 1.19.4 | ~7,160 | 10 | title ~520 |
| Codex 0.164.0 | ~8,330 | 9 | – |
| Qwen Code 0.26.1, orchestrator flags (`qwen_code_tuned`) | ~9,100 | 8 | – |
| Claude Code 2.1.310 (stock, no CLAUDE.md/MCP) | ~10,880 | 12 (rest deferred) | – |
| Crush 0.98.2 | ~11,400 | 26 | title ~140 |
| Qwen Code 0.26.1, `--approval-mode auto-edit` only | ~13,100 | 9 (no shell!) | memory ~5,800 |

How each harness behaves with caching:
- **Stable prefix.** Every harness puts its fixed part (tools, then instructions) first
  and the cwd/date near the end, so harness prefixes are shareable across tasks.
- **Explicit cache markers.** Only Claude Code (Anthropic format) and Pi/dsh with
  `compat.cacheControlFormat: "anthropic"` send `cache_control` markers. Every other
  harness relies on the provider's implicit cache.

Per-task totals depend on turn count far more than on this prefix. That is why the
orchestrator also records real usage per run (`runs.usage`, `source: "harness"`) and
routes from it. See `docs/harness-routing.md`.
