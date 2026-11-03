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

- **Replies** render as Markdown: headings, bold, lists, tables, links and code. Your own
  messages stay as typed.
- **Images and files:** attach with the paperclip button, paste them into the message box,
  or drop them anywhere on the session. See [Images](#images) and [Files](#files).
- **Slash commands:** type `/` to pick from the commands the harness offers. See
  [Slash commands](#slash-commands).
- **Memory:** save a message as a memory entry, or ask the agent to suggest some. See
  [Memory](#memory).
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

## Images

A message can carry up to four PNG, JPEG, GIF or WebP images, each under 5 MB, and images
and files together stay under 10 MB. They're saved in `<home>/chats/<id>/images/`, and the transcript names them rather
than holding them. Deleting the session deletes them.

Images go to the model as images only when the current model takes them; otherwise they're
attached as [files](#files). Which models take images:

- **Pi:** models named in the adapter's `image_models` capability, which defaults to
  `qwen3.8-` and `deepseek-v4`. `glm-5.3` refuses images. Pi itself only passes images
  to models marked this way, including images the agent opens with its `read` tool.
- **Claude Code:** every Claude model.
- **ACP harnesses:** whatever the harness reports when it starts (`promptCapabilities.image`).

On a new session the harness hasn't started yet, so images are sent as images. The first
message waits until the harness reports; if its model doesn't take images, the message isn't
sent and the session shows why.

## Files

Any other file, up to five per message and each under 5 MB, is saved in the session's
working directory under `.orchestrator/attachments/`, numbered so names never clash
(`1-notes.md`). The message the agent gets ends with their paths, and it opens them with its
own tools, so what it can read depends on the harness: text and code always work, while
PDFs and other binary files need a tool that can open them. Your message in the transcript
shows the files by name and stays as typed.

In a git workspace, `.orchestrator/` is excluded locally, so attachments never show up in
**Changes** or reach the branch. In a project checkout they're still written into your
project folder, under that excluded path. Deleting a session with no project deletes its
folder and the files with it; in a worktree they go when the worktree is removed.

## Memory

Chat can add to the same [shared memory](../README.md#how-memory-works) that task prompts use:

- **Save to memory:** hover over a message and click the bookmark. The text opens in a form
  where you trim it and pick the category and scope: global, the session's project, or a
  role. **Approve now** is ticked by default; untick it to leave the entry waiting for review
  on the Memory page.
- **Suggest memories:** the header button asks the agent, in this session, for up to five
  entries worth keeping. It answers in the format task handoffs use, without running tools,
  and the entries wait for review on the Memory page, marked `chat:<id>`. It's one more
  turn of tokens, mostly cached if you ask within five minutes of the last reply. It's
  available while the agent is idle, and not on standalone runs.

Chat sessions don't read memory themselves; approved entries reach task prompts.

## Slash commands

A message is sent to the harness exactly as typed, so a `/command` works whenever the harness
runs it from a prompt. Typing `/` at the start of a message opens a menu of the commands the
harness reports: arrow keys move, **Enter** or **Tab** picks, **Escape** closes. Picking one
fills in its name; add any arguments, then send.

- **Pi:** skills (`/skill:name`), prompt templates and extension commands. Its built-in
  `/mcp` and `/llama` aren't listed, because they open dialogs Chat can't show. Sessions use
  a managed Pi config, but your extensions in `~/.pi/agent/extensions` are linked in, so
  their commands are offered. Project extensions are off (see
  [Permission modes](#permission-modes)).
- **Claude Code:** what its headless mode accepts: skills, custom and plugin commands, and
  built-ins such as `/compact`. Interactive-only commands like `/config` aren't offered.
- **ACP harnesses:** whatever the harness announces. OpenCode lists its skills when the
  session starts; Qwen Code and DeepSeek Harness announce none then, so their menu stays empty,
  though commands typed in full still reach them.

Model, effort and permission are controls under the message box rather than commands.

## MCP servers

Each harness uses the MCP servers in its own config, unless the orchestrator gives it a
temporary one to set its endpoint and key:

- **Pi:** your user-level servers in `~/.pi/agent/mcp.json`, with their sign-ins from
  `mcp-auth.json`, so Qwen, DeepSeek and GLM models can use them. Both files are linked into
  the session's temporary config folder, so a token Pi refreshes is saved back to yours.
  Add servers with `pi mcp add` and sign in with `pi mcp login <name>` in a terminal.
  Project-level `.pi/mcp.json` files aren't read.
- **Claude Code and OpenCode:** your own config, including Claude Code's claude.ai
  connectors.
- **Qwen Code and DeepSeek Harness:** none, because their managed setup runs them with a
  temporary home folder.

By default Pi doesn't give MCP tools to the model directly: the model writes a short
`codemode` script that calls them. In Ask first, the gate asks before the script runs and
again before each MCP tool it calls, so both appear as approvals. Plan blocks both.
Pipeline tasks don't get your Pi servers, because Pi has no gate there; standalone runs do,
and Read and report denies them like any other unrecognised tool.

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
  Your own extensions in `~/.pi/agent/extensions` do load, and their tools pass the gate like
  Pi's. The gate lets a `todo` tool (Pi's example to-do extension) through in every mode,
  since it keeps a list in the session and touches no files.
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

## Context window

The session header shows how full the model's context is: the tokens the latest request
sent, out of the model's window. Claude Code and Pi report each request's size. ACP
harnesses show it only when they send ACP's `usage_update`, which also gives the window.
Otherwise the window comes from the model's profile (Models & Providers, the Context
column), then from a built-in table for the models the orchestrator ships profiles for
(`core/context_windows.py`). A model in neither shows the tokens without a limit.

Pi is told that same window for each model it may use. Without it, Pi assumes 128K and
compacts at about 112K, however much the model holds. Pi compacts before it takes a
message, so the next message can wait minutes for an answer. The transcript shows when
compaction starts and ends, and when it fails. A message sent meanwhile is queued on the
running turn.

## Continuing a session

Sessions live inside the server process, so they end when it stops, and show as
**interrupted** or **closed**. Either kind can be continued: press **Continue**, or just send
a message, and the harness starts again in the same folder or worktree (a worktree that was
cleaned up comes back from its branch). Failed sessions and standalone runs can't be
continued.

- **Claude Code** reopens its conversation with `--resume`.
- **Pi** keeps its conversation in the session's folder (`chats/<id>/harness`) and reloads
  it. Deleting the session deletes it too.
- **ACP harnesses** reload it with `session/load` when they advertise `loadSession`.

Otherwise, or when the restore fails (say Claude Code's saved conversation is gone), the
harness starts fresh and your next message carries the transcript so far: what you and the
agent wrote, with tool output left out and only the newest ~24k tokens kept. That first
message costs the whole transcript in input tokens, with a cold cache. A notice in the
transcript says which way it went. Sessions from before Continue existed always replay.

`POST /api/chat/sessions/{id}/continue` does the same through the API.

## Limits

- Sessions live inside the server process. When it stops they end; see above to continue
  one.
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
{"type": "prompt", "text": "...", "images": [{"data": "<base64>", "mime": "image/png"}],
 "files": [{"name": "notes.md", "data": "<base64>"}]}
{"type": "interrupt"}
{"type": "approval", "id": "...", "choice": "<option id>"}
{"type": "set_model", "value": "..."}
{"type": "set_effort", "value": "..."}
{"type": "set_permission", "value": "..."}
```

`images` and `files` are optional. Transcripts are in `$ORCHESTRATOR_HOME/chats/<id>/events.jsonl`, and a
session's images are served from `GET /api/chat/sessions/{id}/images/{name}`.
