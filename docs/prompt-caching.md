# Prompt caching

All LLM prompts come from `services/prompt_builder.py`, so prompt layout is enforced in
one place.

## Static first, dynamic last

| # | Section | Kind | Cache breakpoint |
|---|---|---|---|
| 1 | `system`: role prompt + output contract | static | ✓ |
| 2 | `global_memory` | static | |
| 3 | `role_memory` | static | |
| 4 | `project_memory` | static | ✓ |
| 5 | `contracts`: project `docs/frozen-contracts.md`, task contracts, out-of-scope | static | |
| 6 | `phase`: task, branch, commands, steps, Definition of Done, acceptance criteria | static | ✓ |
| 7 | `history` | dynamic | |
| 8 | `dynamic_state`: latest handoff, test summary, verification narrative, diff, reviewer feedback | dynamic | |

Dynamic content never appears before static content. A unit test asserts the order, and
the preview flags any violation.

## Memory is part of the cached prefix

Approved memory renders deterministically (sorted by scope, then by entry id), so the
same memory produces byte-identical sections. Approving or editing memory changes the
prefix and costs one cache miss. Do it at **phase boundaries**, not in the middle of a
review loop.

## Cache breakpoints

- **Anthropic**: static sections become separate `system` text blocks. Sections 1, 4 and
  6 (and always the last static block) get `cache_control: {"type": "ephemeral"}`,
  capped at 4 breakpoints per request. Dynamic state is sent as the single user
  message after all breakpoints. Verify hits with `usage.cache_read_input_tokens`, which
  is stored on the run.
- **OpenAI-compatible (Qwen/DeepSeek/GLM/local)**: all static sections are concatenated
  into one stable `system` message. Providers with automatic prefix caching can reuse it,
  and `prompt_tokens_details.cached_tokens` is recorded when the provider returns it.
  No explicit markers are sent. These are single calls minutes apart, and an explicit
  write costs 125% on Qwen Cloud.
- **Harness runs**: Pi and DeepSeek Harness on `qwen3.8-*` send explicit
  `cache_control` markers each turn. See [`harness-routing.md`](harness-routing.md#caching-qwen-cloud--model-studio-context-cache)
  for the Qwen Cloud cache rules and what the orchestrator does with them.

## Truncation rules

Budgets come from `ORCHESTRATOR_MAX_*_TOKENS`, estimated at ~4 characters per token.

- **Diffs**: sensitive files (`.env`, keys, credentials) are stripped first, then the
  middle is truncated to `MAX_DIFF_TOKENS`. Diffs are sent, never whole files.
- **Logs**: the head is dropped (the tail is kept) to fit `MAX_LOG_TOKENS`.
- **Test output**: summarised as `passed/failed/skipped` counts plus `FAILED`/`E` lines,
  never the raw log.
- **Total over `MAX_PROMPT_CONTEXT_TOKENS`**: dynamic sections are cut down to a labelled
  structured summary. Static sections are never truncated.
- Everything passes through secret redaction before reaching a provider.

## Prompt preview

`orchestrator prompt-preview --role worker --task T-1 [--json]`, the GUI **Prompt Cache**
page, or `POST /api/prompt-preview` show each section's kind, breakpoints, approximate
token counts, and the cacheable fraction. They warn when:
- a dynamic section appears before a static one;
- a static section contains a timestamp, UUID, or elapsed-time value.

## Anti-patterns that bust the cache

- Timestamps, run IDs, or elapsed times in the system prompt or memory
- Re-ordering memory entries, or editing memory in the middle of a review loop
- Putting the diff or test output above the Definition of Done
- Switching models between calls (caches are per model) or changing tool sets
- Building JSON with unsorted keys inside static sections (`json_instruction` uses
  `sort_keys=True`)
- Changing the Anthropic `effort` setting on every call for the same conversation
