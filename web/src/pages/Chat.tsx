import { ArrowUp, Bot, ChevronRight, CircleAlert, FileText, FolderOpen, GitBranch, MessageSquarePlus, Paperclip, ShieldAlert, Square, Terminal, TriangleAlert, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import { useNavigate, useParams } from "react-router-dom";
import remarkGfm from "remark-gfm";
import { api, del, getToken, patch, post } from "../api/client";
import type { ChatCatalog, ChatChanges, ChatHarness, ChatSession, PermissionMode, Project } from "../api/types";
import { Badge, ConfirmButton, Empty, ErrorNote, Field, Pre } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";
import { useChatSession, type ChatItem, type SlashCommand } from "../hooks/useChatSession";

const ACTIVE = new Set(["busy", "awaiting_approval"]);
const ENDED = new Set(["closed", "failed", "interrupted"]);

function compact(n: number | undefined): string {
  if (!n) return "0";
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n);
}

const MARKDOWN: Components = {
  pre: ({ children }) => <Pre>{children}</Pre>,
  a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
};

/** Replies as GitHub-flavoured Markdown. Raw HTML in them stays text. */
function Prose({ text }: { text: string }) {
  return <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN}>{text}</Markdown></div>;
}

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
// The server's limits (chat_service); a prompt over them is refused.
const MAX_IMAGES = 4;
const MAX_FILES = 5;
const MAX_ATTACHMENT_BYTES = 5_000_000;
const MAX_ATTACHMENTS_TOTAL = 10_000_000;

interface PromptImage { data: string; mime: string }
interface PromptFile { name: string; data: string }
/** Images go to the model as images; other files are saved where the agent works. */
interface Attachment { image: boolean; name: string; data: string; mime: string; size: number; url?: string }

function readAttachment(file: File, image: boolean): Promise<Attachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      image, name: file.name, data: String(reader.result).split(",", 2)[1] ?? "", mime: file.type, size: file.size,
      url: image ? URL.createObjectURL(file) : undefined,
    });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function baseName(path: string): string {
  return path.split("/").pop() ?? path;
}

/** An image sent in this session. The API needs the token header, so it loads through fetch. */
function ChatImage({ chatId, name }: { chatId: number; name: string }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    let objectUrl: string | undefined;
    let cancelled = false;
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set("X-Orchestrator-Token", token);
    void fetch(`/api/chat/sessions/${chatId}/images/${name}`, { headers })
      .then((r) => (r.ok ? r.blob() : null))
      .then((blob) => {
        if (!blob || cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [chatId, name]);
  if (!url) return <div className="h-24 w-24 animate-pulse rounded-lg bg-panel-2" />;
  return <a href={url} target="_blank" rel="noreferrer"><img src={url} alt="Attached image" className="max-h-48 max-w-full rounded-lg border border-line object-contain" /></a>;
}

function Disclosure({ summary, children, open = false }: { summary: ReactNode; children: ReactNode; open?: boolean }) {
  return (
    <details className="group rounded-xl border border-line bg-panel" open={open}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm text-ink-2 [&::-webkit-details-marker]:hidden">
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

function Transcript({ chatId, items, working, onAnswer, disabled }: { chatId: number; items: ChatItem[]; working: boolean; onAnswer: (id: string, choice: string) => void; disabled: boolean }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [items, working]);
  return (
    <div className="mx-auto max-w-3xl space-y-3 px-6 py-6">
      {items.map((item, i) => {
        switch (item.kind) {
          case "user": return (
            <div key={i} className="ml-auto w-fit max-w-[85%] space-y-2 rounded-2xl bg-accent-soft px-4 py-2.5">
              {item.images?.length ? <div className="flex flex-wrap justify-end gap-2">{item.images.map((name) => <ChatImage key={name} chatId={chatId} name={name} />)}</div> : null}
              {item.files?.length ? (
                <div className="flex flex-wrap justify-end gap-2">
                  {item.files.map((path) => (
                    <span key={path} className="flex max-w-full items-center gap-1.5 rounded-lg border border-line bg-panel px-2 py-1 text-sm" title={path}>
                      <FileText size={14} className="shrink-0 text-muted" aria-hidden="true" /><span className="truncate">{baseName(path)}</span>
                    </span>
                  ))}
                </div>
              ) : null}
              {item.text && <p className="whitespace-pre-wrap break-words text-base leading-relaxed">{item.text}</p>}
            </div>
          );
          case "assistant": return <Prose key={i} text={item.text} />;
          case "thinking": return <Disclosure key={i} summary="Thinking"><p className="whitespace-pre-wrap text-sm text-muted">{item.text}</p></Disclosure>;
          case "tool": return <ToolCard key={i} item={item} />;
          case "approval": return <ApprovalCard key={i} item={item} onAnswer={onAnswer} disabled={disabled} />;
          case "notice": return <div key={i} className="text-center text-sm text-muted">{item.text}</div>;
          case "error": return <div key={i} className="flex gap-2 rounded-xl border border-bad/40 bg-bad/10 px-3 py-2 text-base text-bad"><CircleAlert size={16} className="mt-0.5 shrink-0" aria-hidden="true" /><span className="min-w-0 break-words">{item.text}</span></div>;
        }
      })}
      {working && <div className="animate-pulse text-sm text-muted">Working…</div>}
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

/** Commands matching what's typed after a leading `/`, names starting with it first. */
function matchCommands(commands: SlashCommand[], text: string): SlashCommand[] {
  const typed = /^\/(\S*)$/.exec(text);
  if (!typed) return [];
  const query = typed[1].toLowerCase();
  const starts = commands.filter((c) => c.name.toLowerCase().startsWith(query));
  const contains = commands.filter((c) => !c.name.toLowerCase().startsWith(query) && c.name.toLowerCase().includes(query));
  return [...starts, ...contains];
}

function CommandMenu({ commands, active, onPick, onHover }: {
  commands: SlashCommand[]; active: number; onPick: (c: SlashCommand) => void; onHover: (i: number) => void;
}) {
  const list = useRef<HTMLUListElement>(null);
  useEffect(() => { list.current?.children[active]?.scrollIntoView({ block: "nearest" }); }, [active]);
  return (
    <ul ref={list} id="slash-commands" role="listbox" aria-label="Commands"
      className="absolute inset-x-0 bottom-full mb-2 max-h-72 overflow-y-auto rounded-xl border border-line-strong bg-panel p-1 shadow-(--shadow-card)">
      {commands.map((c, i) => (
        <li key={c.name} id={`slash-${i}`} role="option" aria-selected={i === active}
          className={`cursor-pointer rounded-lg px-3 py-2 ${i === active ? "bg-accent-soft" : ""}`}
          onMouseDown={(e) => { e.preventDefault(); onPick(c); }} onMouseEnter={() => onHover(i)}>
          <div className="mono text-sm">/{c.name}</div>
          {c.description && <div className="truncate text-xs text-muted">{c.description}</div>}
        </li>
      ))}
    </ul>
  );
}

function Composer({ onSend, onStop, working, disabled, images, commands = [], placeholder, controls }: {
  onSend: (text: string, images: PromptImage[], files: PromptFile[]) => void; onStop?: () => void; working: boolean; disabled?: boolean;
  images: boolean | undefined; commands?: SlashCommand[]; placeholder: string; controls: ReactNode;
}) {
  const [text, setText] = useState("");
  const [attached, setAttached] = useState<Attachment[]>([]);
  const [problem, setProblem] = useState("");
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const asImage = (f: File) => images !== false && IMAGE_TYPES.includes(f.type);
  const matches = useMemo(() => matchCommands(commands, text), [commands, text]);
  const menuOpen = !disabled && !dismissed && matches.length > 0;

  const edit = (value: string) => { setText(value); setActive(0); setDismissed(false); };
  const pick = (command: SlashCommand) => { edit(`/${command.name} `); box.current?.focus(); };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return;
    if (menuOpen) {
      const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (step) { e.preventDefault(); setActive((i) => (i + step + matches.length) % matches.length); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); pick(matches[active]); return; }
      if (e.key === "Escape") { e.preventDefault(); setDismissed(true); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  };

  const add = async (picked: File[]) => {
    if (!picked.length) return;
    let problem = "";
    const kept: File[] = [];
    let images = attached.filter((a) => a.image).length;
    let others = attached.length - images;
    let total = attached.reduce((sum, a) => sum + a.size, 0);
    for (const f of picked) {
      const image = asImage(f);
      if (f.size > MAX_ATTACHMENT_BYTES) { problem = `Each attachment must be under ${MAX_ATTACHMENT_BYTES / 1_000_000} MB.`; continue; }
      if (image ? images >= MAX_IMAGES : others >= MAX_FILES) { problem = `At most ${MAX_IMAGES} images and ${MAX_FILES} files per message.`; continue; }
      if (total + f.size > MAX_ATTACHMENTS_TOTAL) { problem = `Attachments in one message must total under ${MAX_ATTACHMENTS_TOTAL / 1_000_000} MB.`; continue; }
      kept.push(f);
      total += f.size;
      if (image) images += 1; else others += 1;
    }
    setProblem(problem);
    const read = await Promise.all(kept.map((f) => readAttachment(f, asImage(f))));
    setAttached((current) => [...current, ...read]);
  };
  const remove = (index: number) => setAttached((current) => {
    const url = current[index].url;
    if (url) URL.revokeObjectURL(url);
    return current.filter((_, i) => i !== index);
  });
  const submit = () => {
    if ((!text.trim() && !attached.length) || disabled) return;
    onSend(text,
      attached.filter((a) => a.image).map(({ data, mime }) => ({ data, mime })),
      attached.filter((a) => !a.image).map(({ name, data }) => ({ name, data })));
    attached.forEach((a) => a.url && URL.revokeObjectURL(a.url));
    setAttached([]);
    setProblem("");
    setText("");
  };
  const files = (list: FileList | null) => [...(list ?? [])];

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-5">
      <div className="relative rounded-2xl border border-line-strong bg-panel shadow-(--shadow-card) focus-within:border-accent"
        onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); }}
        onDrop={(e) => { if (disabled) return; e.preventDefault(); void add(files(e.dataTransfer.files)); }}>
        {attached.length > 0 && (
          <div className="flex flex-wrap gap-2 px-3 pt-3">
            {attached.map((a, i) => (
              <div key={`${i}-${a.name}`} className="group relative">
                {a.url
                  ? <img src={a.url} alt={a.name} className="h-16 w-16 rounded-lg border border-line object-cover" />
                  : <div className="flex h-16 max-w-48 items-center gap-2 rounded-lg border border-line bg-panel-2 px-3 text-sm" title={a.name}>
                      <FileText size={16} className="shrink-0 text-muted" aria-hidden="true" /><span className="truncate">{a.name}</span>
                    </div>}
                <button className="absolute -right-1.5 -top-1.5 rounded-full border border-line-strong bg-panel p-0.5 text-muted hover:text-ink" title="Remove" aria-label={`Remove ${a.name}`} onClick={() => remove(i)}><X size={12} aria-hidden="true" /></button>
              </div>
            ))}
          </div>
        )}
        {menuOpen && <CommandMenu commands={matches} active={active} onPick={pick} onHover={setActive} />}
        <textarea ref={box} aria-label="Message" rows={3} className="block w-full resize-none rounded-t-2xl bg-transparent px-4 pt-3 text-base outline-none placeholder:text-muted disabled:opacity-60"
          role="combobox" aria-expanded={menuOpen} aria-controls={menuOpen ? "slash-commands" : undefined} aria-autocomplete="list"
          aria-activedescendant={menuOpen ? `slash-${active}` : undefined}
          placeholder={placeholder} value={text} disabled={disabled} onChange={(e) => edit(e.target.value)} onBlur={() => setDismissed(true)}
          onPaste={(e) => {
            const pasted = files(e.clipboardData.files);
            if (!pasted.length) return;
            e.preventDefault();
            void add(pasted);
          }}
          onFocus={() => setDismissed(false)} onKeyDown={onKeyDown} />
        {problem && <p className="px-4 pb-1 text-xs text-warn">{problem}</p>}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 pb-3 pt-1">
          <input ref={picker} type="file" multiple hidden onChange={(e) => { void add(files(e.target.files)); e.target.value = ""; }} />
          <button className="btn h-8 w-8 justify-center p-0!" title={images === false ? "Attach files (or paste or drop them). This model doesn't take images, so they go as files." : "Attach images or files (or paste or drop them)"}
            aria-label="Attach files" disabled={disabled || attached.length >= MAX_IMAGES + MAX_FILES} onClick={() => picker.current?.click()}><Paperclip size={15} aria-hidden="true" /></button>
          {controls}
          <div className="ml-auto flex items-center gap-2">
            {working && onStop && <button className="btn btn-danger" title="Stop this turn" onClick={onStop}><Square size={13} aria-hidden="true" />Stop</button>}
            <button className="btn btn-accent h-8 w-8 justify-center p-0!" title="Send" aria-label="Send" disabled={disabled || (!text.trim() && !attached.length)} onClick={submit}><ArrowUp size={16} aria-hidden="true" /></button>
          </div>
        </div>
      </div>
    </div>
  );
}

interface FirstPrompt { text: string; images: PromptImage[]; files: PromptFile[] }

function NewSession({ catalog, projects, onCreated }: { catalog: ChatCatalog; projects: Project[]; onCreated: (chat: ChatSession, firstPrompt: FirstPrompt) => void }) {
  const ready = catalog.harnesses.filter((h) => h.available);
  const preferred = ready.find((h) => h.name === "pi") ?? ready.find((h) => h.kind !== "mock") ?? ready[0];
  const [projectId, setProjectId] = useState<number | null>(projects[0]?.id ?? null);
  const [name, setName] = useState(preferred?.name ?? "");
  const harness = catalog.harnesses.find((h) => h.name === name);
  const [model, setModel] = useState(harness?.models[0] ?? "");
  const [effort, setEffort] = useState(harness?.default_effort ?? "");
  const [permission, setPermission] = useState(harness?.default_permission ?? "");
  const [workspace, setWorkspace] = useState<"worktree" | "checkout">("worktree");
  const [agentId, setAgentId] = useState<number | null>(null);
  const agent = catalog.agents.find((a) => a.id === agentId);
  const act = useAction();

  const pick = (h: ChatHarness, wanted?: string | null) => {
    setName(h.name);
    setModel(wanted ?? h.models[0] ?? "");
    setEffort(h.default_effort ?? "");
    setPermission(h.default_permission ?? "");
  };
  // Agents start from their own route; you can still change any of it below.
  const pickAgent = (id: number | null) => {
    setAgentId(id);
    const a = catalog.agents.find((x) => x.id === id);
    const h = catalog.harnesses.find((x) => x.name === a?.adapter);
    if (!a || !h) return;
    pick(h, h.models.length ? a.model : "");
    if (projects.some((p) => p.id === a.project_id) || a.project_id === null) setProjectId(a.project_id);
  };
  // Claude models live on Claude Code, so choosing one from another harness moves there.
  const claudeHarness = catalog.harnesses.find((h) => h.kind === "claude_cli");
  const claude = claudeHarness?.available ? claudeHarness : undefined;
  const otherModels = claude && claude.name !== name ? claude.models : [];
  const pickModel = (id: string) => {
    if (claude && otherModels.includes(id)) pick(claude, id);
    else setModel(id);
  };
  const start = (prompt: FirstPrompt) => act.run(async () => {
    const chat = await post<ChatSession>("/chat/sessions", { project_id: projectId, agent_id: agentId, adapter: name, model: model || null, effort: effort || null, permission: permission || null, workspace });
    onCreated(chat, prompt);
  });

  return (
    <div className="flex h-full flex-col justify-center">
      <div className="mx-auto w-full max-w-3xl px-6 pb-4">
        <h1 className="mb-1 text-2xl font-bold tracking-tight">New session</h1>
        <p className="mb-5 text-sm text-ink-2">Talk to a coding harness directly. Sessions stay outside the task pipeline, so nothing here is reviewed for you.</p>
        <ErrorNote error={act.error} />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Agent">
            <select className="input" value={agentId ?? ""} onChange={(e) => pickAgent(e.target.value ? Number(e.target.value) : null)}>
              <option value="">No agent</option>
              {catalog.agents.map((a) => <option key={a.id} value={a.id} disabled={!a.adapter} title={a.problem ?? undefined}>{a.name}{a.adapter ? "" : " (unavailable)"}</option>)}
            </select>
          </Field>
          <Field label="Project">
            <select className="input" value={projectId ?? ""} onChange={(e) => setProjectId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">No project</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </Field>
          <Field label="Harness">
            <select className="input" value={name} onChange={(e) => { const h = catalog.harnesses.find((x) => x.name === e.target.value); if (h) pick(h); }}>
              {catalog.harnesses.map((h) => <option key={h.name} value={h.name} disabled={!h.available}>{h.name}{h.available ? "" : " (unavailable)"}</option>)}
            </select>
          </Field>
          <Field label="Workspace">
            {projectId === null
              ? <select className="input" disabled><option>New empty folder</option></select>
              : <select className="input" value={workspace} onChange={(e) => setWorkspace(e.target.value as "worktree" | "checkout")}>
                  <option value="worktree">New worktree (isolated branch)</option>
                  <option value="checkout">Project checkout (your files)</option>
                </select>}
          </Field>
        </div>
        {agent && <p className="mt-2 text-xs text-muted">Its instructions go ahead of your first message. {agent.description}</p>}
        {harness?.note && <p className="mt-2 text-xs text-muted">{harness.note}</p>}
        {claudeHarness && !claude && <p className="mt-2 text-xs text-muted">Claude models aren't available. {claudeHarness.reason}</p>}
        {projectId === null && <p className="mt-2 text-xs text-muted">The agent starts in a new empty folder, which is deleted with the session. The folder isn't a sandbox: the permission mode decides what it can reach elsewhere.</p>}
        {projectId !== null && workspace === "checkout" && <p className="mt-2 flex items-center gap-1.5 text-xs text-warn"><TriangleAlert size={14} aria-hidden="true" />The agent will change your project's files directly, with no branch to review or discard.</p>}
      </div>
      <Composer working={false} disabled={act.busy || !harness?.available} images={undefined} onSend={(text, images, files) => void start({ text, images, files })} placeholder="What should the agent do? Enter to start, Shift+Enter for a new line."
        controls={harness && <>
          <ModelSelect value={model} models={[...harness.models.map((m) => ({ id: m, name: m })), ...otherModels.map((m) => ({ id: m, name: `${m} (Claude Code)` }))]} onChange={pickModel} />
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

function ActiveSession({ chat, harness, initialPrompt, onChanged }: { chat: ChatSession; harness?: ChatHarness; initialPrompt: FirstPrompt | null; onChanged: () => void }) {
  const session = useChatSession(chat.id);
  const { config, status, usage, items, send, live, connected } = session;
  const [showChanges, setShowChanges] = useState(false);
  const act = useAction();
  const navigate = useNavigate();
  const sent = useRef(false);
  const ended = ENDED.has(status);

  // Images wait until the harness has said whether its model takes them.
  useEffect(() => {
    const ready = !initialPrompt?.images.length || config.images !== undefined;
    if (initialPrompt && live && connected && ready && !sent.current) {
      sent.current = true;
      send({ type: "prompt", text: initialPrompt.text, images: initialPrompt.images, files: initialPrompt.files });
    }
  }, [initialPrompt, live, connected, config.images, send]);

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
        {chat.agent_name && <span className="flex items-center gap-1 text-xs text-muted"><Bot size={13} aria-hidden="true" />{chat.agent_name}</span>}
        <span className="text-xs text-muted">{chat.adapter}</span>
        {chat.branch_name
          ? <span className="mono flex items-center gap-1 text-xs text-muted"><GitBranch size={13} aria-hidden="true" />{chat.branch_name}</span>
          : chat.standalone
            ? <span className="text-xs text-muted">standalone run</span>
            : chat.workspace === "folder"
            ? <span className="mono flex min-w-0 items-center gap-1 text-xs text-muted" title={chat.cwd}><FolderOpen size={13} className="shrink-0" aria-hidden="true" /><span className="max-w-xs truncate">{chat.cwd}</span></span>
            : <span className="flex items-center gap-1 text-xs text-warn"><TriangleAlert size={13} aria-hidden="true" />project checkout</span>}
        <div className="ml-auto flex items-center gap-2">
          {usage.requests ? <span className="text-xs text-muted" title={`${compact(usage.cache_read_input_tokens)} of the input came from cache, over ${usage.requests} requests`}>{compact(usage.input_tokens)} in · {compact(usage.output_tokens)} out</span> : null}
          {chat.project_id !== null && <button className="btn" onClick={() => setShowChanges((v) => !v)}>Changes</button>}
          {!ended && <ConfirmButton label="End session" message="Stops the harness process. The transcript and any branch stay." disabled={act.busy}
            onConfirm={() => void act.run(async () => { await post(`/chat/sessions/${chat.id}/close`); onChanged(); })} />}
          {ended && <ConfirmButton label="Delete" message={chat.workspace === "folder" && !chat.standalone ? "Deletes the transcript and this session's folder, with any files in it." : "Deletes the transcript and the worktree. A worktree with uncommitted changes is kept, and the branch always stays."} disabled={act.busy}
            onConfirm={() => void act.run(async () => { await del(`/chat/sessions/${chat.id}`); onChanged(); navigate("/chat"); })} />}
        </div>
      </header>
      {showChanges && <ChangesPanel chat={chat} ended={ended} onDone={onChanged} />}
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto max-w-3xl px-6"><ErrorNote error={act.error} /></div>
        {!items.length && !ended && <Empty>{status === "starting" ? "Starting the harness…" : "Ready."}</Empty>}
        <Transcript chatId={chat.id} items={items} working={working} onAnswer={(id, choice) => send({ type: "approval", id, choice })} disabled={locked} />
      </div>
      {ended
        ? <div className="border-t border-line px-6 py-3 text-center text-xs text-muted">This session has ended ({status.replace(/_/g, " ")}). Its transcript is read-only.</div>
        : chat.standalone
        ? <div className="flex items-center justify-center gap-3 border-t border-line px-6 py-3 text-xs text-muted">
            A standalone run is answering its own approvals, so it can't take instructions.
            <button className="btn" disabled={locked} onClick={() => send({ type: "interrupt" })}>Stop run</button>
          </div>
        : <Composer working={working} disabled={locked} images={config.images} commands={config.commands} onSend={(text, images, files) => send({ type: "prompt", text, images, files })} onStop={() => send({ type: "interrupt" })}
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
  const [firstPrompt, setFirstPrompt] = useState<{ id: number; prompt: FirstPrompt } | null>(null);
  const [created, setCreated] = useState<ChatSession | null>(null);
  const current = sessions.data?.find((c) => c.id === chatId) ?? (created?.id === chatId ? created : undefined);

  useEffect(() => {
    if (chatId != null && current == null) void api<ChatSession>(`/chat/sessions/${chatId}`).then(setCreated).catch(() => navigate("/chat"));
  }, [chatId, current, navigate]);

  const body = () => {
    if (!catalog.data || !projects.data) return <ErrorNote error={catalog.error ?? projects.error} />;
    if (!catalog.data.enabled && !current) return <EnableCard onEnabled={() => void catalog.reload()} />;
    if (current) return <ActiveSession key={current.id} chat={current} harness={catalog.data.harnesses.find((h) => h.name === current.adapter)}
      initialPrompt={firstPrompt?.id === current.id ? firstPrompt.prompt : null} onChanged={() => void sessions.reload()} />;
    return <NewSession catalog={catalog.data} projects={projects.data} onCreated={(chat, prompt) => {
      setCreated(chat);
      setFirstPrompt({ id: chat.id, prompt });
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
                <span className="truncate">{c.agent_name ? `${c.agent_name} · ${c.adapter}` : c.adapter}</span>
              </div>
            </button>
          ))}
        </div>
      </aside>
      <div className="min-w-0 flex-1">{body()}</div>
    </div>
  );
}
