import { useEffect, useState } from "react";
import { patch, post } from "../api/client";
import type { Schedule } from "../api/types";
import { Badge, Card, Empty, ErrorNote, Field, PageHeader, Toggle, fmtTime } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

const DEFAULT_TEMPLATE = JSON.stringify({ title: "Nightly dependency audit", kind: "agent", tags: ["maintenance"], description: "Summarise outdated dependencies." }, null, 2);

export default function Schedules() {
  const { data, error, reload } = useApi<Schedule[]>("/schedules", 10000);
  const [form, setForm] = useState({ name: "", cron: "0 2 * * *", template: DEFAULT_TEMPLATE });
  const [cronCheck, setCronCheck] = useState<{ valid: boolean; next_runs?: string[]; error?: string } | null>(null);
  const act = useAction();

  useEffect(() => {
    const t = setTimeout(() => {
      post<{ valid: boolean; next_runs?: string[]; error?: string }>("/schedules/validate", { cron_expression: form.cron })
        .then(setCronCheck).catch(() => setCronCheck(null));
    }, 300);
    return () => clearTimeout(t);
  }, [form.cron]);

  const create = () => act.run(async () => {
    let template: unknown;
    try { template = JSON.parse(form.template); } catch { throw new Error("task template must be valid JSON"); }
    await post("/schedules", { name: form.name, cron_expression: form.cron, task_template_json: template, enabled: true });
    setForm({ ...form, name: "" });
    await reload();
  });

  return (
    <>
      <PageHeader title="Schedules" subtitle="Cron (UTC) schedules create and queue tasks. Background tasks default to flash-class models." />
      <ErrorNote error={error ?? act.error} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="card overflow-x-auto xl:col-span-2">
          <table className="w-full">
            <thead><tr>{["Name", "Cron", "Enabled", "Last run", "Next run", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>{data?.map((s) => (
              <tr key={s.id}>
                <td className="td font-medium">{s.name}<div className="text-xs text-muted">{String(s.task_template_json.title ?? "")}</div></td>
                <td className="td mono text-xs">{s.cron_expression}</td>
                <td className="td"><Toggle checked={s.enabled} onChange={(v) => act.run(async () => { await patch(`/schedules/${s.id}`, { enabled: v }); await reload(); })} /></td>
                <td className="td text-xs">{fmtTime(s.last_run_at)}</td>
                <td className="td text-xs">{fmtTime(s.next_run_at)}</td>
                <td className="td"><button className="btn" onClick={() => act.run(async () => { await post(`/schedules/${s.id}/run`); await reload(); })}>Run now</button></td>
              </tr>))}</tbody>
          </table>
          {data && !data.length && <Empty>No schedules yet.</Empty>}
        </div>
        <Card title="New schedule">
          <div className="space-y-3">
            <Field label="Name"><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
            <Field label="Cron (min hour dom month dow)"><input className="input mono" value={form.cron} onChange={(e) => setForm({ ...form, cron: e.target.value })} /></Field>
            {cronCheck && (cronCheck.valid
              ? <div className="text-xs text-muted"><Badge value="valid" tone="ok" /> next: {cronCheck.next_runs?.map(fmtTime).join(" · ")}</div>
              : <div className="text-xs text-bad">{cronCheck.error}</div>)}
            <Field label="Task template (JSON, TaskCreate fields)"><textarea className="input mono h-40 text-xs" value={form.template} onChange={(e) => setForm({ ...form, template: e.target.value })} /></Field>
            <button className="btn btn-primary" disabled={!form.name || !cronCheck?.valid} onClick={create}>Create</button>
          </div>
        </Card>
      </div>
    </>
  );
}
