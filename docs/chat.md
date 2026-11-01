# Chat: interactive harness sessions

The **Chat** page talks to a coding harness directly, the way that harness's own GUI does.
Unlike a task, a session has no supervisor, verification or review. Use tasks for work you
want gated and Chat for work you want to steer yourself.

[Standalone runs](agents.md#standalone-runs) also appear here, with a read-only transcript and a
**Stop run** button.

Chat is off by default. Turn it on from the Chat page or with `allow_interactive_sessions`
in Settings. [security.md](security.md#interactive-sessions-chat) explains why.

## Using it

Pick a project, harness, workspace, model, effort and permission mode, then send a message.
**Enter** sends and **Shift+Enter** adds a line.

- **Model, effort and permission** can change during a session, except where noted below.
  Switching models starts a cold prompt cache, because caches are per model.
- **Sending during a turn:** Pi takes the message as steering after the current tool call.
  Other harnesses queue it until the turn ends.
- **Stop** ends the current turn and keeps the session. **End session** stops the process.
- **Changes** shows what the session did. On a worktree session, it also offers the merge,
  which needs the session ended and `merge_enabled` on. Otherwise it shows the manual
  command. Anything uncommitted is committed as a labelled recovery commit first.
- **Delete** removes the transcript and worktree. A dirty worktree stays, and the branch
  always stays.

## Workspaces

- **New worktree** (default): branch `chat/<id>-<project>`, created from the project's base
  branch. Your checkout isn't touched until you merge.
- **Project checkout:** the agent edits your files directly, with no branch to discard.
  `dsh` can't start here if the project's `.env` sets `DEEPSEEK_BASE_URL`, because it refuses
  that variable in a `.env` file.

## Harnesses

| Harness | Protocol | Model | Effort | Permission modes |
|---|---|---|---|---|
| Pi | `--mode rpc` | your profiles | off to high | plan, ask, auto-edit, yolo |
| Qwen Code | ACP (`--acp`) | what it reports | none | plan, default, auto-edit, auto, yolo |
| OpenCode | ACP (`acp`) | what it reports | none | build, plan |
| DeepSeek Harness | ACP (`--profile acp`) | what it reports | off to max | read-only, workspace-write, full access |
| Claude Code | stream-json | your profiles | low to max | plan, manual, acceptEdits, auto, bypass |

ZCode isn't supported: its `app-server` protocol is private. Aider has no session mode.
Claude Code stays disabled until you enable its adapter, and uses your subscription, never
`ANTHROPIC_API_KEY`.

Once a session starts, the harness's own report replaces these lists.

## Permission modes

The same label doesn't mean the same thing on every harness:

- **Pi** has no permission system, so `adapters/chat/pi_gate.ts` adds one through Pi's
  extension UI. Plan mode allows read tools and an allowlist of read-only shell commands.
  Project-local Pi extensions don't load (`--no-approve`), since they could override it.
- **Qwen Code and Claude Code** enforce their own modes. In their ask modes, both let
  read-only commands such as `ls` run without a prompt.
- **OpenCode** allows everything by default, so sessions set `OPENCODE_PERMISSION` to ask for
  edits, shell commands and web fetches. Its `build` mode then asks; `plan` removes edit tools.
- **DeepSeek Harness** sandboxes at the OS level, set by `DSH_PERMISSION_MODE`, so the mode is
  fixed when the session starts. It defaults here to read-only: a blocked write fails, and the
  agent can then ask you to escalate. Its own default, workspace-write, writes in the
  workspace without asking.

## Effort

The slider sends what each harness accepts. On Pi, models on the Token Plan get
`thinkingFormat: qwen`, which is on or off: any level above `off` enables thinking, and the
levels don't otherwise differ. DeepSeek Harness can list levels that a model then refuses; a
refused starting level leaves the default in place and shows a notice. Claude Code has no
live effort control, so a change restarts it between turns with `--resume`, which keeps the
conversation.

## Limits

- Sessions live inside the server process. When the server stops, they end and show as
  **interrupted**, and their transcripts stay readable. They can't be resumed yet.
- At most four sessions run at once.
- Qwen Code's managed setup offers only the model the session started with, so the picker
  has one entry there. OpenCode lists its own models and doesn't use your profiles.
- Usage depends on the harness. Qwen Code reports none over ACP, so its sessions show no
  token counts.

## Protocol reference

`/ws/chat/{id}` first sends `{"type":"history","events":[...],"status":...}`, then live
events, then `{"type":"end"}`. Event shapes are listed in `adapters/chat/base.py`.
Clients send:

```json
{"type": "prompt", "text": "..."}
{"type": "interrupt"}
{"type": "approval", "id": "...", "choice": "<option id>"}
{"type": "set_model", "value": "..."}
{"type": "set_effort", "value": "..."}
{"type": "set_permission", "value": "..."}
```

Transcripts are in `$ORCHESTRATOR_HOME/chats/<id>/events.jsonl`.
