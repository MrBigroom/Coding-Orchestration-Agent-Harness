# Harness routing and token efficiency

Worker runs go to the harness that suits the worker model and costs the fewest tokens,
and the choice is corrected from each run's real usage.

## Why route by harness

All Token Plan models can run in any OpenAI-compatible harness. What changes between
harnesses is how many tokens they spend:

- **Fixed prefix per request.** This is the system prompt, tool schemas and injected
  context. It ranges from ~0.7k (Aider) and ~1.4k (Pi) to ~11k (Claude Code, Crush) and
  ~13k + a ~6k side call (stock Qwen Code). Measured with `scripts/harness-bench/bench.sh`;
  see its README for the table.
- **Turns per task.** This dominates. In Composio's 30-task test on DeepSeek V4 Pro,
  DeepSeek Harness used ~89k tokens per task against ~650k for Claude Code and ~710k for
  OpenCode, with a similar pass rate (20 vs 19 of 30).
- **Quality per model.** Independent same-model, cross-harness results are scarce. Most
  published Qwen3.8 and GLM-5.3 scores were measured inside Claude Code, so the defaults
  below are a starting point and measured data overrides them.

## Order of decisions (`adapter_service.choose_harness`)

1. Explicit `assigned_harness_adapter` on the task.
2. `mock` tags.
3. Harness tags: `deepseek` → DeepSeek Harness, `glm` → ZCode, `qwen` → Qwen Code (when
   installed).
4. **The worker model's route.** The worker profile is chosen first, then its route is
   looked up and the first installed adapter is used:

   | Model prefix | Route |
   |---|---|
   | `deepseek` | deepseek_harness → pi → opencode → aider |
   | `qwen` | pi → qwen_code → opencode → aider |
   | `glm` | pi → opencode → aider |
   | `claude` | claude_cli → opencode |

   Override or extend the routes with the `harness_routing` setting. Keys are a model-name
   prefix (longest match wins) or an exact profile name:

   ```bash
   curl -X PATCH localhost:8321/api/settings -H 'content-type: application/json' \
     -d '{"values": {"harness_routing": {"qwen3.8-max": ["qwen_code", "pi"], "worker-glm": ["opencode"]}}}'
   ```
5. OpenCode, then Aider, then the direct API worker (low risk) or blocked.

`orchestrator task dry-run T-n` shows the chosen harness and the reason.

## Learning from real usage

Each harness run's usage is parsed from the harness's own output and stored in
`runs.usage`:

```json
{"source": "harness", "harness": "pi", "model": "qwen3.8-flash",
 "input_tokens": 41234, "cache_read_input_tokens": 36010, "uncached_input_tokens": 5224,
 "output_tokens": 1830, "requests": 14}
```

`input_tokens` always includes cached tokens, whatever the harness reports natively.

For each (harness, model) pair, `usage_service.harness_stats` sums **weighted tokens**
over all runs, including failed runs and CHANGES_REQUESTED rounds, and divides by the
number of distinct **accepted** tasks. So a harness that needs extra review rounds, or
fails, pays for it.

Weighted tokens are `uncached + 0.2 × cached + 3 × output`. These weights approximate
Model Studio's relative credit cost: an implicit cache hit costs 20% of input, and
qwen3.8-max lists $2 / $6 per 1M tokens. Alibaba doesn't publish the Token Plan's
credits-per-token rates, so treat this as a ranking, not a bill.

Once **at least two** installed candidates on the route each have **≥ 3 accepted tasks**
on the model, the one with the lowest weighted tokens per accepted task wins. The reason
string shows the numbers. Before that, the configured order holds. There's no automatic
exploration. To gather data for a new pairing, assign the harness on a few tasks
(`--harness pi`), started by hand, which fits the Token Plan's "no high-frequency
automation" condition.

## Caching (Qwen Cloud / Model Studio context cache)

Facts from Qwen Cloud's context-cache docs (checked 2026-10-26):
- **Implicit cache:** always on and free to enable. Hits are billed at **20%** of the
  input price, but a hit isn't guaranteed. The minimum prefix is model-dependent
  (256–2,000 tokens, depending on the doc page and model).
- **Explicit cache:** `cache_control: {"type": "ephemeral"}`, minimum **1,024** tokens,
  TTL **5 min** (reset on every hit). Writing costs **125%** of input and hits cost
  **10%**. At most **4** markers per request, and the lookback reaches only **20**
  content blocks back from a marker.
- **Models:** explicit caching works on `qwen3.8-max` and `qwen3.8-flash`. DeepSeek V4.x
  and GLM are implicit-only on Qwen Cloud.
- **Matching:** caches are **per model**. Tools are cached as part of the system prompt
  and must be byte-identical between requests, order included.
- **Check hits:** `usage.prompt_tokens_details.cached_tokens` (OpenAI format) or
  `cache_read_input_tokens` (Anthropic format).

What the orchestrator does with this:
- **Explicit markers where they pay off.** Pi and DeepSeek Harness runs on `qwen3.8-*`
  models get `cacheControlFormat: "anthropic"`, which marks the system prompt, last tool
  and newest message (3 markers). That is Qwen Cloud's recommended pattern for
  multi-turn agents: each turn reads the previous turn's cache at 10% instead of maybe
  20%. Agent turns come seconds apart, well inside the 5-minute TTL, and the cached
  prefix per turn is much larger than the new tokens per turn. So the 25% write premium
  on new tokens is outweighed by the extra 10% saved on the cached part.
- **No markers for DeepSeek/GLM.** They are implicit-only, so markers are never sent.
- **Stable harness prefixes.** Every measured harness keeps tools and instructions
  first and puts the cwd/date near the end. Runs of the same harness and model share
  their prefix across tasks while the cache is warm.
- **Fewer cache-breaking side calls.** Qwen Code's memory-extraction call is disabled.
  It used a different prefix and cost about 6k tokens per run.
- **Stable model per route.** Routing keeps a profile on one model and harness, which
  matters because caches are per model.
- **Cache hit rate in the stats.** Recorded usage includes `cache_read_input_tokens`,
  so `harness_stats` reports a `cache_hit_rate` per (harness, model). That's how to
  check whether explicit caching is actually honoured on your Token Plan endpoint.

What it deliberately doesn't do:
- **No explicit markers on the orchestrator's own calls** (supervisor, agent tasks,
  direct worker). They are single calls minutes apart, usually outside the 5-minute TTL,
  so a 125% write would rarely be reused. The implicit cache costs nothing extra.
  Revisit this if you run many tasks back to back.
- **No session resume between review rounds.** A CHANGES_REQUESTED round starts a fresh
  harness session. Rounds are usually minutes apart, so a resumed session's cache would
  have expired anyway, and a fresh session keeps the prompt deterministic.

Open questions to verify on your account:
- **Explicit cache on the Token Plan endpoint.** Run a Pi task on `qwen3.8-flash` and
  check that `cache_read_input_tokens` on the run is large.
- **Claude Code via Token Plan.** It could use the Anthropic-compatible endpoint
  (`…/apps/anthropic`), where it sends explicit markers itself. It has the heaviest
  prefix, though, so it isn't routed by default.
