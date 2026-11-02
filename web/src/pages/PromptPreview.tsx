import { useState } from "react";
import { post } from "../api/client";
import type { CacheStatsRow, PreviewReport, Task } from "../api/types";
import { Badge, Card, Empty, ErrorNote, Field, PageHeader } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

const SOURCE: Record<CacheStatsRow["source"], [string, string]> = {
  task: ["Tasks", "runs"],
  chat: ["Chat", "sessions"],
  standalone: ["Standalone", "runs"],
};

function tokens(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

/** Cache hits recorded so far, from harness runs of tasks, Chat sessions and standalone runs. */
function CacheStats() {
  const { data, error } = useApi<{ rows: CacheStatsRow[] }>("/cache-stats");
  const rows = data?.rows ?? [];
  return (
    <Card title="Cache hit rate" className="mb-4">
      <ErrorNote error={error} />
      {data && !rows.length ? <Empty>No harness usage recorded yet.</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th className="pb-2 pr-4 font-medium">Source</th>
                <th className="pb-2 pr-4 font-medium">Harness · model</th>
                <th className="pb-2 pr-4 text-right font-medium">Requests</th>
                <th className="pb-2 pr-4 text-right font-medium">Input</th>
                <th className="pb-2 pr-4 text-right font-medium">From cache</th>
                <th className="w-48 pb-2 font-medium">Hit rate</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((r) => {
                const [label, unit] = SOURCE[r.source];
                const pct = Math.round(r.cache_hit_rate * 100);
                return (
                  <tr key={`${r.source}-${r.harness}-${r.model}`}>
                    <td className="py-2 pr-4">{label}<div className="text-xs text-muted">{r.count} {r.count === 1 ? unit.slice(0, -1) : unit}</div></td>
                    <td className="py-2 pr-4">{r.harness}<div className="mono text-xs text-muted">{r.model ?? "—"}</div></td>
                    <td className="py-2 pr-4 text-right tabular-nums">{r.requests || "—"}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{tokens(r.input_tokens)}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{tokens(r.cached_input_tokens)}</td>
                    <td className="py-2">
                      <div className="flex items-center gap-2">
                        <div className="h-1.5 flex-1 rounded bg-panel-2"><div className={`h-1.5 rounded ${pct >= 85 ? "bg-ok" : pct >= 60 ? "bg-warn" : "bg-bad"}`} style={{ width: `${pct}%` }} /></div>
                        <span className="w-10 text-right font-medium tabular-nums">{pct}%</span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-3 text-xs text-muted">Qwen Cloud keeps a cache for 5 minutes after its last use, so a Chat message sent after a longer pause starts with a full miss. Tool calls within a turn come seconds apart and usually hit.</p>
        </div>
      )}
    </Card>
  );
}

export default function PromptPreview() {
  const { data: tasks } = useApi<Task[]>("/tasks");
  const [role, setRole] = useState("worker");
  const [taskId, setTaskId] = useState("");
  const [report, setReport] = useState<PreviewReport | null>(null);
  const act = useAction();
  const load = () => act.run(async () => setReport(await post<PreviewReport>("/prompt-preview", { role, task_id: taskId ? Number(taskId) : null })));
  const max = Math.max(1, ...(report?.sections.map((s) => s.tokens) ?? [1]));

  return (
    <>
      <PageHeader title="Prompt cache" subtitle="How often the cache is hit, and how each prompt is laid out: static sections form the cached prefix, dynamic state comes last." />
      <CacheStats />
      <ErrorNote error={act.error} />
      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Role"><select className="input w-44" value={role} onChange={(e) => setRole(e.target.value)}>{["architect", "supervisor", "worker", "agent"].map((r) => <option key={r}>{r}</option>)}</select></Field>
          <Field label="Task (optional)"><select className="input w-80" value={taskId} onChange={(e) => setTaskId(e.target.value)}><option value="">—</option>{tasks?.map((t) => <option key={t.id} value={t.id}>{t.short_id} · {t.title}</option>)}</select></Field>
          <button className="btn btn-primary" onClick={load}>Preview</button>
        </div>
      </Card>
      {report && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            {[["Static ≈tokens", report.static_tokens], ["Dynamic ≈tokens", report.dynamic_tokens], ["Total", report.total_tokens], ["Cacheable", `${Math.round(report.cacheable_fraction * 100)}%`]].map(([k, v]) => (
              <div key={k} className="card px-4 py-3"><div className="text-xs uppercase tracking-wide text-muted">{k}</div><div className="mt-1 text-xl font-semibold">{v}</div></div>
            ))}
          </div>
          {report.warnings.length > 0 ? report.warnings.map((w, i) => (
            <div key={i} className="mb-2 rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">⚠ {w}</div>
          )) : <div className="mb-3 rounded-md border border-ok/40 bg-ok/10 px-3 py-2 text-sm text-ok">Ordering OK: all static sections precede dynamic state.</div>}
          <Card title="Sections (in send order)">
            <ol className="space-y-2">
              {report.sections.map((s) => (
                <li key={s.index} className={`rounded-md border border-line p-3 ${s.empty ? "opacity-50" : ""}`}>
                  <div className="mb-1 flex items-center gap-2 text-sm">
                    <span className="mono w-6 text-muted">{s.index + 1}</span>
                    <span className="font-medium">{s.name}</span>
                    <Badge value={s.static ? "static" : "dynamic"} tone={s.static ? "ok" : "warn"} />
                    {s.cache_breakpoint && <Badge value="cache breakpoint" tone="info" />}
                    <span className="ml-auto text-xs text-muted">{s.empty ? "empty" : `≈${s.tokens} tokens`}</span>
                  </div>
                  <div className="h-1.5 rounded bg-panel-2"><div className={`h-1.5 rounded ${s.static ? "bg-ok/60" : "bg-warn/60"}`} style={{ width: `${(s.tokens / max) * 100}%` }} /></div>
                  {!s.empty && <pre className="mono mt-2 max-h-28 overflow-auto whitespace-pre-wrap text-xs text-muted">{s.excerpt}</pre>}
                </li>
              ))}
            </ol>
          </Card>
        </>
      )}
    </>
  );
}
