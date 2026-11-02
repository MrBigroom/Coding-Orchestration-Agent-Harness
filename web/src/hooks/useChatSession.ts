import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../api/client";
import type { ChatUsage, PermissionMode } from "../api/types";

export interface ApprovalOption {
  id: string;
  label: string;
  tone: "allow" | "deny";
}

export type ChatItem =
  | { kind: "user"; text: string; images?: string[] }
  | { kind: "assistant"; text: string }
  | { kind: "thinking"; text: string }
  | { kind: "tool"; id: string; name: string; input: unknown; output?: string; error?: boolean; done: boolean }
  | { kind: "approval"; id: string; title: string; detail: string; options: ApprovalOption[]; resolved?: boolean; choice?: string | null }
  | { kind: "notice" | "error"; text: string };

export interface ChatConfig {
  model?: string | null;
  effort?: string | null;
  permission?: string | null;
  models?: { id: string; name: string }[];
  efforts?: string[];
  permissions?: PermissionMode[];
  permission_locked?: boolean;
  images?: boolean;
  commands?: SlashCommand[];
}

/** A command the harness runs when a message starts with `/name`. */
export interface SlashCommand {
  name: string;
  description: string;
}

interface ChatState {
  items: ChatItem[];
  status: string;
  config: ChatConfig;
  usage: Partial<ChatUsage>;
}

type ChatEvent = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const EMPTY: ChatState = { items: [], status: "starting", config: {}, usage: {} };

function appendDelta(items: ChatItem[], kind: "assistant" | "thinking", delta: string): ChatItem[] {
  const last = items[items.length - 1];
  if (last?.kind === kind) return [...items.slice(0, -1), { ...last, text: last.text + delta }];
  return [...items, { kind, text: delta }];
}

function apply(state: ChatState, e: ChatEvent): ChatState {
  const items = state.items;
  switch (e.type) {
    case "user": return { ...state, items: [...items, { kind: "user", text: e.text, images: e.images }] };
    case "text": return { ...state, items: appendDelta(items, "assistant", e.delta) };
    case "thinking": return { ...state, items: appendDelta(items, "thinking", e.delta) };
    case "tool_start":
      return { ...state, items: [...items, { kind: "tool", id: e.id, name: e.name, input: e.input, done: false }] };
    case "tool_end":
      return { ...state, items: items.map((i) => (i.kind === "tool" && i.id === e.id ? { ...i, output: e.output, error: e.error, done: true } : i)) };
    case "approval":
      return { ...state, items: [...items, { kind: "approval", id: e.id, title: e.title, detail: e.detail, options: e.options }] };
    case "approval_resolved":
      return { ...state, items: items.map((i) => (i.kind === "approval" && i.id === e.id && !i.resolved ? { ...i, resolved: true, choice: e.choice } : i)) };
    case "status": return { ...state, status: e.status };
    case "usage": return { ...state, usage: e };
    case "config": {
      const { type: _type, seq: _seq, ...config } = e;
      const defined = Object.fromEntries(Object.entries(config).filter(([, v]) => v !== undefined));
      return { ...state, config: { ...state.config, ...defined } };
    }
    case "notice": return { ...state, items: [...items, { kind: "notice", text: e.message }] };
    case "error": return { ...state, items: [...items, { kind: "error", text: e.message }] };
    default: return state;
  }
}

/** One session's transcript and live channel. Reconnects while the session is alive. */
export function useChatSession(chatId: number | null) {
  const [state, setState] = useState<ChatState>(EMPTY);
  const [connected, setConnected] = useState(false);
  const [live, setLive] = useState(false);
  const socket = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (chatId == null) return;
    setState(EMPTY);
    setLive(false);
    let stopped = false;
    let ended = false;
    let retry: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      const ws = new WebSocket(wsUrl(`/ws/chat/${chatId}`));
      socket.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data as string) as ChatEvent;
        if (msg.type === "history") {
          setState((msg.events as ChatEvent[]).reduce(apply, { ...EMPTY, status: msg.status }));
          setLive(true);
        } else if (msg.type === "end") {
          ended = true;
          setLive(false);
        } else {
          setState((s) => apply(s, msg));
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!stopped && !ended) retry = setTimeout(connect, 1500);
      };
    };
    connect();
    return () => {
      stopped = true;
      socket.current?.close();
      socket.current = null;
      if (retry) clearTimeout(retry);
    };
  }, [chatId]);

  const send = useCallback((message: ChatEvent) => {
    socket.current?.send(JSON.stringify(message));
  }, []);

  return { ...state, connected, live, send };
}
