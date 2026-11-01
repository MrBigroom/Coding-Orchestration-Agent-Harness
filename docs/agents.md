# Agents and scheduled tasks

An **agent** is a saved, reusable definition: a name, standing instructions, a model
profile, and optionally a project and a harness. Running it, by hand or from a schedule,
creates a task. Agents add no new execution path. Every run goes through the same task
machinery as a task you create yourself.

## What a run does

| Agent has a project? | Task kind | Path |
|---|---|---|
| yes | `coding` | Isolated worktree on a phase branch → the agent's harness → Supervisor verification (your test/lint commands) → Architect review → merge gate (off by default) |
| no | `agent` | The model answers directly through its provider API. There is no repo to edit, so no harness |

The task gets:

- `description` = the agent's instructions, then the request. The instructions come first
  so repeated runs of the same agent share a cacheable prompt prefix.
- `assigned_provider_profile` = the agent's model (empty means normal routing).
- `assigned_harness_adapter` = the agent's harness. Empty means the harness is routed
  from the model, as described in [`harness-routing.md`](harness-routing.md): Pi first for
  Qwen, DeepSeek, GLM and Kimi, and so on, with the measured-efficiency override.
- tags = the agent's tags + `agent:<name>`, and `agent_id` = the agent.

The Agents page (and `GET /api/agents/{id}`) shows which model and harness the next run
would use, and why.

## Scheduled tasks

A schedule fires on a 5-field cron (UTC) and does one of two things:

- **Runs an agent** (`agent_id` set): the template's `description` is the request.
  `title`, `risk`, `definition_of_done` and `queue` are optional. Model, harness and
  project come from the agent.
- **Creates a task** (no agent): the template is a `TaskCreate` body, as before.

Scheduled tasks are queued, and the queue worker runs them like any other task.

**Token Plan.** Its terms exclude high-frequency automated use (see
[`providers.md`](providers.md)). When a schedule would run a Token Plan model more often
than once a day, `POST /api/schedules/validate`, the GUI and `orchestrator schedule add`
all show a warning. It's a warning, not a block.

## CLI

```bash
orchestrator agent add deps --project myapp --model worker-deepseek-flash \
    --instructions "Keep dependencies current; never bump a major version silently."
orchestrator agent list                      # shows the routed model + harness
orchestrator agent run deps "Update the patch-level dependencies"
orchestrator schedule add weekly-deps "0 3 * * 1" --agent deps \
    --template '{"description": "Update patch-level dependencies"}'
```

## API

`GET/POST /api/agents`, `GET/PATCH/DELETE /api/agents/{id}`, `POST /api/agents/{id}/run`
(`{"request", "title"?, "risk"?, "definition_of_done"?, "queue"?}`),
`DELETE /api/schedules/{id}`. Schedules accept an optional `agent_id`. In a PATCH,
`agent_id: 0` detaches the agent, an empty `model_profile`/`harness_adapter` restores
automatic routing, and `project_id: 0` clears the project.

You can't delete an agent while a schedule uses it. Deleting an agent keeps its past tasks.
