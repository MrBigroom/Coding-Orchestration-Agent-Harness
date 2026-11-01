import { ArrowUp, ChevronRight, CircleAlert, GitBranch, MessageSquarePlus, ShieldAlert, Square, Terminal, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, del, patch, post } from "../api/client";
import type { ChatCatalog, ChatChanges, ChatHarness, ChatSession, PermissionMode, Project } from "../api/types";
import { Badge, ConfirmButton, Empty, ErrorNote, Field, Pre } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";
import { useChatSession, type ChatItem } from "../hooks/useChatSession";

const ACTIVE = new Set(["busy", "awaiting_approval"]);
const ENDED = new Set(["closed", "failed", "interrupted"]);

function compact(n: number | undefined): string {
  if (!n) return "0";
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n);
}

/** Prose with fenced code blocks; everything else keeps its line breaks. */
function Prose({ text }: { text: string }) {
  const parts = text.split(/```[^\n]*\n?/);
  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {parts.map((part, i) =>
        i % 2 ? <Pre key={i}>{part.replace(/\n$/, "")}</Pre> : part.trim() ? <p key={i} className="whitespace-pre-wrap break-words">{part.trim()}</p> : null)}
    </div>
  );
}

function Disclosure({ summary, children, open = false }: { summary: ReactNode; children: ReactNode; open?: boolean }) {
  return (
    <details className="group rounded-xl border border-line bg-panel" open={open}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs text-ink-2 [&::-webkit-details-marker]:hidden">
        <ChevronRight size={14} className="shrink-0 transition-transform group-open:rotate-90" aria-hidden="true" />
        {summary}
      </summary>
      <div className="space-y-2 border-t border-line p-3">{children}</div>
    </details>
  );
}

function formatInput(input: unknown): string {
  if (input == null) return "";
  return typeof input === "string" ? input : JSON.stringify(input, null, 2);
}

function ToolCard({ item }: { item: Extract<ChatItem, { kind: "tool" }> }) {
  const state = !item.done ? "running" : item.error ? "failed" : "completed";
  return (
    <Disclosure summary={<>
      <Terminal size={14} className="shrink-0" aria-hidden="true" />
      <span className="mono min-w-0 truncate">{item.name}</span>
      <span className="ml-auto"><Badge value={state} /></span>
    </>}>
      {formatInput(item.input) && <Pre>{formatInput(item.input)}</Pre>}
      {item.output && <Pre>{item.output}</Pre>}
    </Disclosure>
  );
}

function ApprovalCard({ item, onAnswer, disabled }: { item: Extract<ChatItem, { kind: "approval" }>; onAnswer: (id: string, choice: string) => void; disabled: boolean }) {
  if (item.resolved) {
    const label = item.options.find((o) => o.id === item.choice)?.label;
    return <div className="text-xs text-muted">{item.title} — {label ?? "cancelled"}</div>;
  }
  return (
    <div className="rounded-xl border border-warn/50 bg-warn/10 p-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">
        <ShieldAlert size={16} className="shrink-0 text-warn" aria-hidden="true" />
        {item.title}
      </div>
      {item.detail && <Pre className="mb-3 max-h-60">{item.detail}</Pre>}
      <div className="flex flex-wrap gap-2">
        {item.options.map((o) => (
          <button key={o.id} disabled={disabled} className={`btn ${o.tone === "deny" ? "btn-danger" : "btn-primary"}`} onClick={() => onAnswer(item.id, o.id)}>
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function Transcript({ items, working, onAnswer, disabled }: { items: ChatItem[]; working: boolean; onAnswer: (id: string, choice: string) => void; disabled: boolean }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [items, working]);
  return (
    <div className="mx-auto max-w-3xl space-y-3 px-6 py-6">
      {items.map((item, i) => {
        switch (item.kind) {
          case "user": return <div key={i} className="ml-auto w-fit max-w-[85%] rounded-2xl bg-accent-soft px-4 py-2.5"><Prose text={item.text} /></div>;
          case "assistant": return <Prose key={i} text={item.text} />;
          case "thinking": return <Disclosure key={i} summary="Thinking"><p className="whitespace-pre-wrap text-xs text-muted">{item.text}</p></Disclosure>;
          case "tool": return <ToolCard key={i} item={item} />;
          case "approval": return <ApprovalCard key={i} item={item} onAnswer={onAnswer} disabled={disabled} />;
          case "notice": return <div key={i} className="text-center text-xs text-muted">{item.text}</div>;
          case "error": return <div key={i} className="flex gap-2 rounded-xl border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad"><CircleAlert size={16} className="mt-0.5 shrink-0" aria-hidden="true" /><span className="min-w-0 break-words">{item.text}</span></div>;
        }
      })}
      {working && <div className="animate-pulse text-xs text-muted">Working…</div>}
      <div ref={end} />
    </div>
  );
}

function ModelSelect({ value, models, onChange, disabled }: { value: string; models: { id: string; name: string }[]; onChange: (v: string) => void; disabled?: boolean }) {
  const options = models.some((m) => m.id === value) || !value ? models : [{ id: value, name: value }, ...models];
  return (
    <select aria-label="Model" title="Model" className="input w-auto max-w-56 py-1 text-xs" value={value} disabled={disabled || !options.length} onChange={(e) => onChange(e.target.value)}>
      {!value && <option value="">Harness default</option>}
      {options.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
    </select>
  );
}

function EffortSlider({ value, efforts, onChange, disabled }: { value: string; efforts: string[]; onChange: (v: string) => void; disabled?: boolean }) {
  if (!efforts.length) return <span className="text-xs text-muted" title="This harness has no effort setting">No effort control</span>;
  const index = Math.max(0, efforts.indexOf(value));
  return (
    <label className="flex items-center gap-2 text-xs text-ink-2">
      Effort
      <input type="range" min={0} max={efforts.length - 1} step={1} value={index} disabled={disabled} className="w-24 accent-accent" aria-valuetext={efforts[index]}
        onChange={(e) => onChange(efforts[Number(e.target.value)])} />
      <span className="w-14 font-medium text-ink">{efforts[index]}</span>
    </label>
  );
}

function PermissionSelect({ value, modes, onChange, disabled }: { value: string; modes: PermissionMode[]; onChange: (v: string) => void; disabled?: boolean }) {
  const [confirming, setConfirming] = useState<PermissionMode | null>(null);
  if (!modes.length) return <span className="text-xs text-muted">No permission modes</span>;
  return (
    <>
      <select aria-label="Permission mode" className="input w-auto py-1 text-xs" value={value} disabled={disabled}
        title={modes.find((m) => m.id === value)?.description ?? "Permission mode"}
        onChange={(e) => {
          const mode = modes.find((m) => m.id === e.target.value);
          if (mode?.dangerous) setConfirming(mode);
          else onChange(e.target.value);
        }}>
        {modes.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
      </select>
      {confirming && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setConfirming(null)}>
          <div role="dialog" aria-modal="true" className="card w-full max-w-md p-5 shadow-(--shadow-pop)" onClick={(e) => e.stopPropagation()}>
            <h3 className="mb-2 flex items-center gap-2 font-semibold"><TriangleAlert size={18} className="text-bad" aria-hidden="true" />Run every tool without asking?</h3>
            <p className="mb-5 text-sm text-muted">The agent can run any command as your user, and nothing will stop to ask you first. A worktree limits where it starts, not what it can reach.</p>
            <div className="flex justify-end gap-2">
              <button className="btn" autoFocus onClick={() => setConfirming(null)}>Cancel</button>
              <button className="btn btn-danger" onClick={() => { onChange(confirming.id); setConfirming(null); }}>Turn on {confirming.label}</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function Composer({ onSend, onStop, working, disabled, placeholder, controls }: {
  onSend: (text: string) => void; onStop?: () => void; working: boolean; disabled?: boolean; placeholder: string; controls: ReactNode;
}) {
  const [text, setText] = useState("");
  const submit = () => {
    if (!text.trim() || disabled) return;
    onSend(text);
    setText("");
  };
  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-5">
      <div className="rounded-2xl border border-line-strong bg-panel shadow-(--shadow-card) focus-within:border-accent">
        <textarea aria-label="Message" rows={3} className="block w-full resize-none rounded-t-2xl bg-transparent px-4 pt-3 text-sm outline-none placeholder:text-muted disabled:opacity-60"
          placeholder={placeholder} value={text} disabled={disabled} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 pb-3 pt-1">
          {controls}
          <div className="ml-auto flex items-center gap-2">
            {working && onStop && <button className="btn btn-danger" title="Stop this turn" onClick={onStop}><Square size={13} aria-hidden="true" />Stop</button>}
            <button className="btn btn-accent h-8 w-8 justify-center p-0!" title="Send" aria-label="Send" disabled={disabled || !text.trim()} onClick={submit}><ArrowUp size={16} aria-hidden="true" /></button>
          </div>
        </div>
      </div>
    </div>
  );
}

function NewSession({ catalog, projects, onCreated }: { catalog: ChatCatalog; projects: Project[]; onCreated: (chat: ChatSession, firstPrompt: string) => void }) {
  const ready = catalog.harnesses.filter((h) => h.available);
  const preferred = ready.find((h) => h.name === "pi") ?? ready.find((h) => h.kind !== "mock") ?? ready[0];
  const [projectId, setProjectId] = useState<number | null>(projects[0]?.id ?? null);
  const [name, setName] = useState(preferred?.name ?? "");
  const harness = catalog.harnesses.find((h) => h.name === name);
  const [model, setModel] = useState(harness?.models[0] ?? "");
  const [effort, setEffort] = useState(harness?.default_effort ?? "");
  const [permission, setPermission] = useState(harness?.default_permission ?? "");
  const [workspace, setWorkspace] = useState<"worktree" | "checkout">("worktree");
  const act = useAction();

  const pick = (h: ChatHarness) => {
    setName(h.name);
    setModel(h.models[0] ?? "");
    setEffort(h.default_effort ?? "");
    setPermission(h.default_permission ?? "");
  };
  const start = (text: string) => act.run(async () => {
    const chat = await post<ChatSession>("/chat/sessions", { project_id: projectId, adapter: name, model: model || null, effort: effort || null, permission: permission || null, workspace });
    onCreated(chat, text);
  });

  if (!projects.length) return <Empty>Add a project first. A session needs a repository to work in.</Empty>;
  return (
    <div className="flex h-full flex-col justify-center">
      <div className="mx-auto w-full max-w-3xl px-6 pb-4">
        <h1 className="mb-1 text-2xl font-bold tracking-tight">New session</h1>
        <p className="mb-5 text-sm text-ink-2">Talk to a coding harness directly. Sessions stay outside the task pipeline, so nothing here is reviewed for you.</p>
        <ErrorNote error={act.error} />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Project">
            <select className="input" value={projectId ?? ""} onChange={(e) => setProjectId(Number(e.target.value))}>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </Field>
          <Field label="Harness">
            <select className="input" value={name} onChange={(e) => { const h = catalog.harnesses.find((x) => x.name === e.target.value); if (h) pick(h); }}>
              {catalog.harnesses.map((h) => <option key={h.name} value={h.name} disabled={!h.available}>{h.name}{h.available ? "" : " (unavailable)"}</option>)}
            </select>
          </Field>
          <Field label="Workspace">
            <select className="input" value={workspace} onChange={(e) => setWorkspace(e.target.value as "worktree" | "checkout")}>
              <option value="worktree">New worktree (isolated branch)</option>
              <option value="checkout">Project checkout (your files)</option>
            </select>
          </Field>
        </div>
        {harness?.note && <p className="mt-2 text-xs text-muted">{harness.note}</p>}
        {workspace === "checkout" && <p className="mt-2 flex items-center gap-1.5 text-xs text-warn"><TriangleAlert size={14} aria-hidden="true" />The agent will change your project's files directly, with no branch to review or discard.</p>}
      </div>
      <Composer working={false} disabled={act.busy || !harness?.available} onSend={(t) => void start(t)} placeholder="What should the agent do? Enter to start, Shift+Enter for a new line."
        controls={harness && <>
          <ModelSelect value={model} models={harness.models.map((m) => ({ id: m, name: m }))} onChange={setModel} />
          <EffortSlider value={effort} efforts={harness.efforts} onChange={setEffort} />
          <PermissionSelect value={permission} modes={harness.permissions} onChange={setPermission} />
        </>} />
    </div>
  );
}

function ChangesPanel({ chat, ended, onDone }: { chat: ChatSession; ended: boolean; onDone: () => void }) {
  const { data, error, reload } = useApi<ChatChanges>(`/chat/sessions/${chat.id}/changes`);
  const act = useAction();
  return (
    <div className="border-b border-line bg-panel-2 px-6 py-4">
      <div className="mx-auto max-w-3xl space-y-3 text-sm">
        <ErrorNote error={error ?? act.error} />
        {data && !data.isolated && <p className="text-muted">This session worked directly in the project checkout, so there is no branch to merge.</p>}
        {data?.uncommitted && <div><div className="label">Uncommitted</div><Pre>{data.uncommitted}</Pre></div>}
        {data?.diffstat && <div><div className="label">Committed against {data.base_branch}</div><Pre>{data.diffstat}</Pre></div>}
        {data && !data.uncommitted && !data.diffstat && <p className="text-muted">No changes yet.</p>}
        {data?.isolated && (
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn" onClick={() => void reload()}>Refresh</button>
            {data.merge_enabled
              ? <ConfirmButton label="Merge" danger={false} disabled={!ended || act.busy} message={`Commit anything left over and merge ${data.branch} into ${data.base_branch}. The base checkout must be on that branch and clean.`}
                  onConfirm={() => void act.run(async () => { await post(`/chat/sessions/${chat.id}/merge`); await reload(); onDone(); })} />
              : <span className="text-xs text-muted">Merging from the GUI is off. To merge by hand: <span className="mono">{data.merge_command}</span></span>}
            {data.merge_enabled && !ended && <span className="text-xs text-muted">End the session to merge.</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function ActiveSession({ chat, harness, initialPrompt, onChanged }: { chat: ChatSession; harness?: ChatHarness; initialPrompt: string | null; onChanged: () => void }) {
  const session = useChatSession(chat.id);
  const { config, status, usage, items, send, live, connected } = session;
  const [showChanges, setShowChanges] = useState(false);
  const act = useAction();
  const navigate = useNavigate();
  const sent = useRef(false);
  const ended = ENDED.has(status);

  useEffect(() => {
    if (initialPrompt && live && connected && !sent.current) {
      sent.current = true;
      send({ type: "prompt", text: initialPrompt });
    }
  }, [initialPrompt, live, connected, send]);

  const prevStatus = useRef(status);
  useEffect(() => {
    if (prevStatus.current !== status) { prevStatus.current = status; onChanged(); }
  }, [status, onChanged]);

  const models = config.models ?? (harness?.models ?? []).map((m) => ({ id: m, name: m }));
  const efforts = config.efforts ?? harness?.efforts ?? [];
  const permissions = useMemo(() => {
    const known = new Map((harness?.permissions ?? []).map((m) => [m.id, m]));
    return (config.permissions ?? harness?.permissions ?? []).map((m) => ({ ...known.get(m.id), ...m, dangerous: known.get(m.id)?.dangerous }));
  }, [config.permissions, harness]);
  const working = ACTIVE.has(status);
  const locked = !live || !connected;

  return (
    <div className="flex h-full min-w-0 flex-col">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-6 py-3">
        <h1 className="min-w-0 max-w-md truncate text-sm font-semibold">{chat.title || "New session"}</h1>
        <Badge value={status} tone={status === "awaiting_approval" ? "warn" : status === "idle" ? "ok" : undefined} />
        <span className="text-xs text-muted">{chat.adapter}</span>
        {chat.branch_name
          ? <span className="mono flex items-center gap-1 text-xs text-muted"><GitBranch size={13} aria-hidden="true" />{chat.branch_name}</span>
          : chat.standalone
            ? <span className="text-xs text-muted">standalone run</span>
            : <span className="flex items-center gap-1 text-xs text-warn"><TriangleAlert size={13} aria-hidden="true" />project checkout</span>}
        <div className="ml-auto flex items-center gap-2">
          {usage.requests ? <span className="text-xs text-muted" title={`${compact(usage.cache_read_input_tokens)} of the input came from cache, over ${usage.requests} requests`}>{compact(usage.input_tokens)} in · {compact(usage.output_tokens)} out</span> : null}
          {chat.project_id !== null && <button className="btn" onClick={() => setShowChanges((v) => !v)}>Changes</button>}
          {!ended && <ConfirmButton label="End session" message="Stops the harness process. The transcript and any branch stay." disabled={act.busy}
            onConfirm={() => void act.run(async () => { await post(`/chat/sessions/${chat.id}/close`); onChanged(); })} />}
          {ended && <ConfirmButton label="Delete" message="Deletes the transcript and the worktree. A worktree with uncommitted changes is kept, and the branch always stays." disabled={act.busy}
            onConfirm={() => void act.run(async () => { await del(`/chat/sessions/${chat.id}`); onChanged(); navigate("/chat"); })} />}
        </div>
      </header>
      {showChanges && <ChangesPanel chat={chat} ended={ended} onDone={onChanged} />}
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto max-w-3xl px-6"><ErrorNote error={act.error} /></div>
        {!items.length && !ended && <Empty>{status === "starting" ? "Starting the harness…" : "Ready."}</Empty>}
        <Transcript items={items} working={working} onAnswer={(id, choice) => send({ type: "approval", id, choice })} disabled={locked} />
      </div>
      {ended
        ? <div className="border-t border-line px-6 py-3 text-center text-xs text-muted">This session has ended ({status.replace(/_/g, " ")}). Its transcript is read-only.</div>
        : chat.standalone
        ? <div className="flex items-center justify-center gap-3 border-t border-line px-6 py-3 text-xs text-muted">
            A standalone run is answering its own approvals, so it can't take instructions.
            <button className="btn" disabled={locked} onClick={() => send({ type: "interrupt" })}>Stop run</button>
          </div>
        : <Composer working={working} disabled={locked} onSend={(text) => send({ type: "prompt", text })} onStop={() => send({ type: "interrupt" })}
            placeholder={working ? "Send more instructions…" : "Message the agent. Shift+Enter for a new line."}
            controls={<>
              <ModelSelect value={config.model ?? chat.model ?? ""} models={models} disabled={locked || status === "starting"} onChange={(value) => send({ type: "set_model", value })} />
              <EffortSlider value={config.effort ?? chat.effort ?? ""} efforts={efforts} disabled={locked || status === "starting"} onChange={(value) => send({ type: "set_effort", value })} />
              <PermissionSelect value={config.permission ?? chat.permission ?? ""} modes={permissions} disabled={locked || status === "starting" || config.permission_locked} onChange={(value) => send({ type: "set_permission", value })} />
            </>} />}
    </div>
  );
}

function EnableCard({ onEnabled }: { onEnabled: () => void }) {
  const act = useAction();
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="card max-w-lg p-6">
        <h1 className="mb-2 flex items-center gap-2 text-lg font-semibold"><ShieldAlert size={20} className="text-warn" aria-hidden="true" />Interactive sessions are off</h1>
        <p className="mb-2 text-sm text-ink-2">Chat runs coding agents that can read files and run shell commands as your user. Requests from other websites are refused, and new sessions default to asking first.</p>
        <p className="mb-4 text-sm text-ink-2">Sessions skip the supervisor and review gate that tasks go through.</p>
        <ErrorNote error={act.error} />
        <button className="btn btn-primary" disabled={act.busy} onClick={() => void act.run(async () => { await patch("/settings", { values: { allow_interactive_sessions: true } }); onEnabled(); })}>Turn on Chat</button>
      </div>
    </div>
  );
}

export default function Chat() {
  const { id } = useParams();
  const navigate = useNavigate();
  const chatId = id ? Number(id) : null;
  const catalog = useApi<ChatCatalog>("/chat/harnesses");
  const projects = useApi<Project[]>("/projects");
  const sessions = useApi<ChatSession[]>("/chat/sessions", 5000);
  const [firstPrompt, setFirstPrompt] = useState<{ id: number; text: string } | null>(null);
  const [created, setCreated] = useState<ChatSession | null>(null);
  const current = sessions.data?.find((c) => c.id === chatId) ?? (created?.id === chatId ? created : undefined);

  useEffect(() => {
    if (chatId != null && current == null) void api<ChatSession>(`/chat/sessions/${chatId}`).then(setCreated).catch(() => navigate("/chat"));
  }, [chatId, current, navigate]);

  const body = () => {
    if (!catalog.data || !projects.data) return <ErrorNote error={catalog.error ?? projects.error} />;
    if (!catalog.data.enabled && !current) return <EnableCard onEnabled={() => void catalog.reload()} />;
    if (current) return <ActiveSession key={current.id} chat={current} harness={catalog.data.harnesses.find((h) => h.name === current.adapter)}
      initialPrompt={firstPrompt?.id === current.id ? firstPrompt.text : null} onChanged={() => void sessions.reload()} />;
    return <NewSession catalog={catalog.data} projects={projects.data} onCreated={(chat, text) => {
      setCreated(chat);
      setFirstPrompt({ id: chat.id, text });
      void sessions.reload();
      navigate(`/chat/${chat.id}`);
    }} />;
  };

  return (
    <div className="-mx-8 -my-7 flex h-[calc(100%+3.5rem)]">
      <aside className="flex w-60 shrink-0 flex-col border-r border-line">
        <div className="p-3"><button className="btn w-full justify-center" onClick={() => navigate("/chat")}><MessageSquarePlus size={15} aria-hidden="true" />New session</button></div>
        <div className="min-h-0 flex-1 space-y-0.5 overflow-auto px-2 pb-2">
          {sessions.data?.map((c) => (
            <button key={c.id} onClick={() => navigate(`/chat/${c.id}`)}
              className={`block w-full rounded-xl px-3 py-2 text-left transition-colors ${c.id === chatId ? "bg-accent-soft" : "hover:bg-panel-2"}`}>
              <div className="truncate text-sm">{c.title || "New session"}</div>
              <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${c.status === "awaiting_approval" ? "bg-warn" : ACTIVE.has(c.status) ? "bg-accent" : c.status === "failed" ? "bg-bad" : ENDED.has(c.status) ? "bg-line-strong" : "bg-ok"}`} />
                {c.adapter}
              </div>
            </button>
          ))}
        </div>
      </aside>
      <div className="min-w-0 flex-1">{body()}</div>
    </div>
  );
}
