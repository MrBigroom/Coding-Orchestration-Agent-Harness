import { Check, Copy } from "lucide-react";
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { highlight, langLabel, langForPath, resolveLang, styleOf, useCodeThemes, type Highlighted, type Tokens } from "../highlight";

/** Highlighted lines once ready; plain text until then, and for unknown languages. */
function useHighlight(code: string, lang: string | null): Highlighted | null {
  const themes = useCodeThemes();
  const [result, setResult] = useState<{ key: string; value: Highlighted | null } | null>(null);
  const resolved = resolveLang(lang);
  const key = `${resolved}|${themes.light}|${themes.dark}|${code}`;
  useEffect(() => {
    let live = true;
    highlight(code, resolved, themes)
      .then((value) => live && setResult({ key, value }))
      .catch(() => live && setResult({ key, value: null }));
    return () => { live = false; };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return result?.key === key ? result.value : null;
}

function Line({ tokens, text }: { tokens?: Tokens[number]; text: string }) {
  if (!tokens) return <>{text || " "}</>;
  if (!tokens.length) return <> </>;
  return <>{tokens.map((t, i) => <span key={i} className="code-tok" style={t.htmlStyle as CSSProperties}>{t.content}</span>)}</>;
}

/** The Clipboard API can be refused (some webviews, no permission), so fall back to the
 * older select-and-copy. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.cssText = "position:fixed;opacity:0;pointer-events:none";
    document.body.appendChild(area);
    area.select();
    try {
      return document.execCommand("copy");
    } catch {
      return false;
    } finally {
      area.remove();
    }
  }
}

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  const copy = () => void copyText(text).then((ok) => {
    setState(ok ? "done" : "failed");
    setTimeout(() => setState("idle"), 1500);
  });
  return (
    <button type="button" onClick={copy} className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted hover:bg-panel-2 hover:text-ink" aria-label="Copy code">
      {state === "done" ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
      {state === "done" ? "Copied" : state === "failed" ? "Couldn't copy" : "Copy"}
    </button>
  );
}

function Frame({ label, copy, rootStyle, className = "", children }: { label: string; copy: string; rootStyle?: string; className?: string; children: ReactNode }) {
  return (
    <div className={`code-block mono overflow-hidden rounded-md border border-line text-xs ${className}`} style={rootStyle ? styleOf(rootStyle) : undefined}>
      <div className="code-bar flex items-center justify-between gap-2 border-b border-line px-2 py-1">
        <span className="text-[11px] text-muted">{label}</span>
        <CopyButton text={copy} />
      </div>
      {children}
    </div>
  );
}

/** A block of code with line numbers, its language and a copy button. `lang` is a fence
 * name, alias or extension; without a known one the block shows as plain text. */
export function Code({ code, lang, path, className = "", maxHeight = "max-h-[32rem]" }: {
  code: string; lang?: string | null; path?: string | null; className?: string; maxHeight?: string;
}) {
  const language = resolveLang(lang) ?? langForPath(path);
  const text = code.replace(/\n$/, "");
  const hl = useHighlight(text, language);
  const lines = useMemo(() => text.split("\n"), [text]);
  const numbers = lines.length > 1;
  return (
    <Frame label={language ? langLabel(language) : lang || "text"} copy={text} rootStyle={hl?.rootStyle} className={className}>
      <div className={`${maxHeight} overflow-auto py-2 leading-relaxed`}>
        {lines.map((l, i) => (
          <div key={i} className="flex">
            {numbers && <span className="code-gutter w-10 shrink-0 select-none pr-3 text-right tabular-nums" aria-hidden="true">{i + 1}</span>}
            <span className={`min-w-0 flex-1 whitespace-pre-wrap break-words ${numbers ? "pr-3" : "px-3"}`}><Line tokens={hl?.tokens[i]} text={l} /></span>
          </div>
        ))}
      </div>
    </Frame>
  );
}

interface DiffRow { kind: "file" | "meta" | "hunk" | "add" | "del" | "ctx"; text: string; old?: number; new?: number; file?: number; body?: number }

/** Unified-diff rows, with each file's body lines numbered in order for highlighting. */
function parseDiff(diff: string): { rows: DiffRow[]; files: { path: string; body: string[] }[] } {
  const rows: DiffRow[] = [];
  const files: { path: string; body: string[] }[] = [];
  let file: { path: string; body: string[] } | null = null;
  let oldNo = 0;
  let newNo = 0;
  const body = (kind: DiffRow["kind"], text: string, o?: number, n?: number) => {
    if (!file) { file = { path: "", body: [] }; files.push(file); }
    rows.push({ kind, text, old: o, new: n, file: files.indexOf(file), body: file.body.length });
    file.body.push(text);
  };
  for (const line of diff.replace(/\n$/, "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      file = { path: line.split(" b/").pop() ?? "", body: [] };
      files.push(file);
      rows.push({ kind: "file", text: file.path });
    } else if (line.startsWith("@@")) {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
      oldNo = m ? Number(m[1]) : 0;
      newNo = m ? Number(m[2]) : 0;
      rows.push({ kind: "hunk", text: line });
    } else if (line.startsWith("+++ ") || line.startsWith("--- ") || !/^[ +-]/.test(line)) {
      rows.push({ kind: "meta", text: line });
    } else if (line[0] === "+") body("add", line.slice(1), undefined, newNo++);
    else if (line[0] === "-") body("del", line.slice(1), oldNo++, undefined);
    else body("ctx", line.slice(1), oldNo++, newNo++);
  }
  return { rows, files };
}

const ROW_TINT: Partial<Record<DiffRow["kind"], string>> = { add: "diff-add", del: "diff-del" };

function FileTokens({ path, body, onReady }: { path: string; body: string[]; onReady: (t: Highlighted | null) => void }) {
  const hl = useHighlight(body.join("\n"), langForPath(path));
  useEffect(() => onReady(hl), [hl]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

/** A unified diff: added and removed lines tinted, old and new line numbers, and each
 * file's code highlighted in its own language. */
export function Diff({ diff, className = "", maxHeight = "max-h-[40rem]" }: { diff: string; className?: string; maxHeight?: string }) {
  const { rows, files } = useMemo(() => parseDiff(diff), [diff]);
  const [tokens, setTokens] = useState<Record<number, Highlighted | null>>({});
  let rootStyle: string | undefined;
  for (const t of Object.values(tokens)) rootStyle ??= t?.rootStyle;
  return (
    <Frame label="Diff" copy={diff} rootStyle={rootStyle} className={className}>
      {files.map((f, i) => <FileTokens key={i} path={f.path} body={f.body} onReady={(t) => setTokens((s) => ({ ...s, [i]: t }))} />)}
      <div className={`${maxHeight} overflow-auto py-1 leading-relaxed`}>
        {rows.map((r, i) => {
          if (r.kind === "file") return <div key={i} className="code-bar mt-1 border-y border-line px-3 py-1 font-semibold first:mt-0">{r.text}</div>;
          if (r.kind === "meta") return <div key={i} className="code-gutter px-3">{r.text}</div>;
          if (r.kind === "hunk") return <div key={i} className="diff-hunk px-3">{r.text}</div>;
          const line = tokens[r.file ?? -1]?.tokens[r.body ?? -1];
          return (
            <div key={i} className={`flex ${ROW_TINT[r.kind] ?? ""}`}>
              <span className="code-gutter w-10 shrink-0 select-none pr-2 text-right tabular-nums" aria-hidden="true">{r.old ?? ""}</span>
              <span className="code-gutter w-10 shrink-0 select-none pr-2 text-right tabular-nums" aria-hidden="true">{r.new ?? ""}</span>
              <span className="code-gutter w-4 shrink-0 select-none text-center" aria-hidden="true">{r.kind === "add" ? "+" : r.kind === "del" ? "−" : ""}</span>
              <span className="min-w-0 flex-1 whitespace-pre-wrap break-words pr-3"><Line tokens={line} text={r.text} /></span>
            </div>
          );
        })}
      </div>
    </Frame>
  );
}

/** True when `text` looks like a unified diff. */
export function looksLikeDiff(text: string): boolean {
  return /^diff --git /m.test(text) || (/^--- /m.test(text) && /^\+\+\+ /m.test(text) && /^@@ /m.test(text));
}

/** The language of a tool's output or input, guessed from its content and any file path. */
export function guessLang(text: string, path?: string | null): string | null {
  const t = text.trimStart();
  if (t.startsWith("{") || t.startsWith("[")) {
    try { JSON.parse(text); return "json"; } catch { /* not JSON */ }
  }
  return langForPath(path);
}
