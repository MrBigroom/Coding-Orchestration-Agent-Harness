import { useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../api/client";

export interface LogLine {
  stream: "stdout" | "stderr";
  line: string;
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "timeout"]);

/** Live run logs via WebSocket; falls back to HTTP polling if the socket fails. */
export function useRunLogs(runId: number | null) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [mode, setMode] = useState<"ws" | "polling" | "idle">("idle");
  const stopped = useRef(false);

  useEffect(() => {
    if (runId == null) return;
    stopped.current = false;
    setLines([]);
    setStatus(null);
    let ws: WebSocket | null = null;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let gotMessage = false;

    const startPolling = () => {
      if (stopped.current) return;
      setMode("polling");
      setLines([]);
      const offsets = { stdout: 0, stderr: 0 };
      const tick = async () => {
        try {
          for (const stream of ["stdout", "stderr"] as const) {
            const r = await api<{ data: string; offset: number; status: string }>(
              `/logs/${runId}?stream=${stream}&offset=${offsets[stream]}`,
            );
            offsets[stream] = r.offset;
            setStatus(r.status);
            if (r.data) {
              const chunk = r.data.split("\n").filter((l) => l.length > 0).map((line) => ({ stream, line }));
              setLines((prev) => [...prev, ...chunk]);
            }
            if (TERMINAL.has(r.status) && stream === "stderr") return;
          }
        } catch {
          /* keep trying */
        }
        if (!stopped.current) pollTimer = setTimeout(tick, 1500);
      };
      void tick();
    };

    try {
      ws = new WebSocket(wsUrl(`/ws/runs/${runId}/logs`));
      setMode("ws");
      ws.onmessage = (ev) => {
        gotMessage = true;
        const msg = JSON.parse(ev.data as string);
        if (msg.type === "log") setLines((prev) => [...prev, { stream: msg.stream, line: msg.line }]);
        else if (msg.type === "status" || msg.type === "end") setStatus(msg.status);
      };
      ws.onerror = () => {
        if (!gotMessage) startPolling();
      };
      ws.onclose = (ev) => {
        if (!gotMessage && ev.code !== 1000) startPolling();
      };
    } catch {
      startPolling();
    }

    return () => {
      stopped.current = true;
      ws?.close();
      if (pollTimer) clearTimeout(pollTimer);
    };
  }, [runId]);

  return { lines, status, mode };
}
