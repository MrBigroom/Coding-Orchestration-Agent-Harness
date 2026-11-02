# Chat: interactive harness sessions

The **Chat** page talks to a coding harness directly, the way that harness's own GUI does.
Unlike a task, a session has no supervisor, verification or review. Use tasks for work you
want gated and Chat for work you want to steer yourself.

[Standalone runs](agents.md#standalone-runs) also appear here, with a read-only transcript and a
**Stop run** button.

Chat is off by default. Turn it on from the Chat page or with `allow_interactive_sessions`
in Settings. [security.md](security.md#interactive-sessions-chat) explains why.

## Using it

Pick a project (or **No project**), harness, workspace, model, effort and permission mode, then
send a message. You can also pick an agent; see [Chatting with an agent](#chatting-with-an-agent).
**Enter** sends and **Shift+Enter** adds a line.

- **Model, effort and permission** can change during a session, except where noted below.
  Switching models starts a cold prompt cache, because caches are per model.
- **Sending during a turn:** Pi takes the message as steering after the current tool call.
  Other harnesses queue it until the turn ends.
- **Stop** ends the current turn and keeps the session. **End session** stops the process.
- **Changes** shows what a project session did. On a worktree session, it also offers the merge,
  which needs the session ended and `merge_enabled` on. Otherwise it shows the manual
  command. Anything uncommitted is committed as a labelled recovery commit first.
- **Delete** removes the transcript and worktree. A dirty worktree stays, and the branch
  always stays. A session with no project loses its folder too, including any files in it.

## Workspaces

- **New worktree** (default): branch `chat/<id>-<project>`, created from the project's base
  branch. Your checkout isn't touched until you merge.
- **Project checkout:** the agent edits your files directly, with no branch to discard.
  `dsh` can't start here if the project's `.env` sets `DEEPSEEK_BASE_URL`, because it refuses
  that variable in a `.env` file.
- **No project:** the agent starts in a new empty folder, `<home>/chats/<id>/workspace`, shown
  in the session header. It isn't a git repository, so there's no branch, **Changes** or merge.

## Chatting with an agent

Choosing a saved agent under **Agent** fills in its project, harness and model, which you can
still change. Its instructions go ahead of your first message, so the agent keeps them for the
whole session, but they stay out of the transcript. The agent's name shows on the session.

Unlike a [standalone run](agents.md#standalone-runs), you're there to answer approvals, so a
chat with an agent uses the permission mode you pick. Editing an agent doesn't change sessions
already under way. Disabled agents aren't listed, and an agent with no usable harness shows as
unavailable.

## Claude models

The model list offers Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5, whether or not a profile
names them. They run through Claude Code, so picking one while another harness is selected
moves the session to Claude Code. A running session can't change harness. Claude models need
the `claude_cli` adapter turned on in Adapters. Fable 5.1 also needs usage credits on your Claude
plan; without them, the session shows Claude Code's error and you can switch model.

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
