# Agents and scheduled tasks

An **agent** is a saved, reusable definition: a name, standing instructions, a model
profile, and optionally a project and a harness. Running it, by hand or from a pipeline
schedule, creates a task that goes through the same machinery as a task you create yourself.
A [standalone schedule](#standalone-runs) runs the agent on its own instead.

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

Pipeline schedules are queued, and the queue worker runs them like any other task.

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

## Standalone runs

An agent schedule can be **Standalone** instead of **Pipeline**. A standalone run starts a
harness session directly, with no task, Supervisor, verification or review. It costs no
review tokens, and an agent with no project still gets the harness's tools.

Turn it on with `allow_unattended_sessions` in Settings. It's off by default and stays on one
device, and a run started while it's off is recorded as skipped. The server must be running,
and a run ends if the server stops. Run these from the GUI or the API; `orchestrator schedule
run` only fires pipeline schedules.

**Where it works**

| | Agent has a project | No project |
|---|---|---|
| **Read and report** | A throwaway detached copy of the base branch, removed afterwards | The output folder |
| **Can make changes** | A worktree on its own `chat/<id>-<project>` branch, kept for review | The output folder |

**What it may do.** Nobody can answer an approval, so the run answers them itself:

| Request | Read and report | Can make changes |
|---|---|---|
| Reading | Allowed | Allowed |
| Writing a file | Output folder only | Working directory and output folder |
| Shell commands, web fetches, anything else | Denied | Allowed |
| A request it can't identify | Denied | Allowed |

Denied requests are listed in the report under **Blocked**. If a denial ends the agent's turn
before it has reported, which Qwen Code and OpenCode do, the run tells it to carry on, up to
three times. Some harnesses decide things
before asking: Qwen Code and Claude Code run read-only commands without a prompt. Pi uses the
same read-only command list as Plan mode. In Read and report, DeepSeek Harness's OS sandbox and
OpenCode's workspace limit both stop writes to the output folder, so only the report is saved. See
[security.md](security.md#standalone-runs) for what these limits do and don't guarantee.

**Output.** Each agent has a folder at `<home>/agent-outputs/<agent>/`. The agent's last message
is saved to `reports/<date>-<time>-run<id>.md`, with any blocked actions and errors, and
secrets are redacted. The Agents page lists each run under **Outputs**, with its status, tokens,
report and any files it wrote. **Session** opens the transcript on the Chat page. It's read-only,
and only **Stop run** works while the run is going.

**Limits.** Each run has a time limit (default 30 minutes) and ends when the agent's first turn
ends. A run is skipped if the same schedule's previous run is still going, or if two standalone
runs are already active. These runs don't use Chat's four session slots. Harnesses are chosen
from the agent's route, and those without a session protocol (ZCode, Aider) are passed over.
Only the agent's own harness setting can make a run fail for that reason.

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
orchestrator schedule add daily-report "0 8 * * *" --agent deps --run-mode standalone \
    --access read --time-limit 15 --template '{"description": "Report outdated dependencies"}'
```

## API

`GET/POST /api/agents`, `GET/PATCH/DELETE /api/agents/{id}`, `POST /api/agents/{id}/run`
(`{"request", "title"?, "risk"?, "definition_of_done"?, "queue"?}`),
`DELETE /api/schedules/{id}`. Schedules accept an optional `agent_id`, a `timezone`, and
either `cron_expression` or `run_at`. A `run_at` with no offset is read in `timezone`, and
`POST /api/schedules/validate` takes the same fields. Schedules also take `run_mode`
(`pipeline` or `standalone`), `access` (`read` or `write`) and `time_limit_minutes`. Standalone
runs are at `GET /api/agents/{id}/outputs`, `GET /api/standalone-runs/{id}/report` and
`POST /api/standalone-runs/{id}/stop`. A PATCH can also change `name`, and
edits only affect future runs. In a PATCH, setting `cron_expression` or `run_at` clears the
other, `agent_id: 0` detaches the agent, an empty `model_profile`/`harness_adapter` restores
automatic routing, and `project_id: 0` clears the project.

You can't delete an agent while a schedule uses it. Deleting an agent keeps its past tasks.
