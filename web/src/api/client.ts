// Thin fetch wrapper. All URLs are relative (/api/...), so the same build works behind
// the FastAPI server or the Vite dev proxy. Optional token auth via X-Orchestrator-Token.

const TOKEN_KEY = "orchestrator.token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token lives for this page only */
  }
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("X-Orchestrator-Token", token);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const res = await fetch(`/api${path}`, { ...init, headers, body });
  const text = await res.text();
  let data: unknown = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* plain text response (artifacts) */
  }
  if (!res.ok) {
    const err = data as { error?: string; detail?: unknown } | null;
    if (res.status === 401) window.dispatchEvent(new CustomEvent("orchestrator:unauthorized"));
    throw new ApiError(res.status, err?.error ?? res.statusText, err?.detail);
  }
  return data as T;
}

export const post = <T = unknown>(path: string, json?: unknown) => api<T>(path, { method: "POST", json: json ?? {} });
export const patch = <T = unknown>(path: string, json: unknown) => api<T>(path, { method: "PATCH", json });

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const token = getToken();
  return `${proto}//${location.host}${path}${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}
