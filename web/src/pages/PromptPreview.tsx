import { useState } from "react";
import { post } from "../api/client";
import type { PreviewReport, Task } from "../api/types";
import { Badge, Card, ErrorNote, Field, PageHeader } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

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
      <PageHeader title="Prompt cache preview" subtitle="Static sections form the cached prefix; dynamic state must come last." />
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
