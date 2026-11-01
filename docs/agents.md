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

A schedule either repeats on a 5-field cron or runs once at a date and time, and each run
does one of two things:

- **Runs an agent** (`agent_id` set): the template's `description` is the request.
  `title`, `risk`, `definition_of_done` and `queue` are optional. Model, harness and
  project come from the agent.
- **Creates a task** (no agent): the template is a `TaskCreate` body, as before.

Scheduled tasks are queued, and the queue worker runs them like any other task.

**Time zones.** Each schedule stores an IANA `timezone`, so a repeating run keeps its local
time across daylight-saving changes. The GUI uses your browser's zone. The API and CLI default
to UTC.

**Weekdays.** Cron weekdays follow the crontab standard: 0 and 7 are Sunday, and 1 is Monday.

**One-time schedules.** Set `run_at` instead of a cron. A time in the past is refused. Once
the schedule has run it turns itself off and stays in the list. To run it again, edit it and
choose a new time. If the server is off at that time, it runs at the next startup and the build log
marks it as late. Repeating schedules skip runs they missed. **Run now** on a one-time schedule
adds an extra run and leaves the planned one in place.

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
orchestrator schedule add release-check --at "2026-11-20 09:00" --timezone Asia/Kuala_Lumpur \
    --agent deps --template '{"description": "Check the release branch"}'
```

## API

`GET/POST /api/agents`, `GET/PATCH/DELETE /api/agents/{id}`, `POST /api/agents/{id}/run`
(`{"request", "title"?, "risk"?, "definition_of_done"?, "queue"?}`),
`DELETE /api/schedules/{id}`. Schedules accept an optional `agent_id`, a `timezone`, and
either `cron_expression` or `run_at`. A `run_at` with no offset is read in `timezone`, and
`POST /api/schedules/validate` takes the same fields. A PATCH can also change `name`, and
edits only affect future runs. In a PATCH, setting `cron_expression` or `run_at` clears the
other, `agent_id: 0` detaches the agent, an empty `model_profile`/`harness_adapter` restores
automatic routing, and `project_id: 0` clears the project.

You can't delete an agent while a schedule uses it. Deleting an agent keeps its past tasks.
