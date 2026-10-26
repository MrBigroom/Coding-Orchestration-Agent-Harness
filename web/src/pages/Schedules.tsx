import { useEffect, useState } from "react";
import { del, patch, post } from "../api/client";
import type { Agent, ModelProfile, Project, Schedule } from "../api/types";
import { Badge, Card, ConfirmButton, Empty, ErrorNote, Field, PageHeader, Toggle, fmtTime } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

type CronCheck = { valid: boolean; next_runs?: string[]; error?: string; warning?: string | null };

const PRESETS: [string, string][] = [
  ["Daily 02:00", "0 2 * * *"],
  ["Weekdays 09:00", "0 9 * * 1-5"],
  ["Weekly, Mon 03:00", "0 3 * * 1"],
  ["Monthly, 1st 04:00", "0 4 1 * *"],
];

const BLANK = { name: "", mode: "agent" as "agent" | "task", agent_id: "", title: "", request: "",
  model_profile: "", project_id: "", cron: "0 2 * * *", advanced: false, template: "{}" };

function buildTemplate(f: typeof BLANK): Record<string, unknown> {
  if (f.advanced) return JSON.parse(f.template);
  if (f.mode === "agent") return { description: f.request, ...(f.title ? { title: f.title } : {}) };
  return {
    title: f.title || f.request.split("\n")[0].slice(0, 80),
    description: f.request,
    project_id: f.project_id ? Number(f.project_id) : null,
    kind: f.project_id ? "coding" : "agent",
    ...(f.model_profile ? { assigned_provider_profile: f.model_profile } : {}),
  };
}

export default function Schedules() {
  const { data, error, reload } = useApi<Schedule[]>("/schedules", 10000);
  const agents = useApi<Agent[]>("/agents");
  const profiles = useApi<ModelProfile[]>("/model-profiles");
  const projects = useApi<Project[]>("/projects");
  const [f, setF] = useState(BLANK);
  const [cronCheck, setCronCheck] = useState<CronCheck | null>(null);
  const act = useAction();
  const set = (k: keyof typeof BLANK) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });

  useEffect(() => {
    const t = setTimeout(() => {
      post<CronCheck>("/schedules/validate", {
        cron_expression: f.cron,
        agent_id: f.mode === "agent" && f.agent_id ? Number(f.agent_id) : null,
        model_profile: f.mode === "task" ? f.model_profile || null : null,
      }).then(setCronCheck).catch(() => setCronCheck(null));
    }, 300);
    return () => clearTimeout(t);
  }, [f.cron, f.mode, f.agent_id, f.model_profile]);

  const create = () => act.run(async () => {
    let template: Record<string, unknown>;
    try { template = buildTemplate(f); } catch { throw new Error("task template must be valid JSON"); }
    await post("/schedules", {
      name: f.name, cron_expression: f.cron, task_template_json: template, enabled: true,
      agent_id: f.mode === "agent" ? Number(f.agent_id) : null,
    });
    setF({ ...BLANK, mode: f.mode, agent_id: f.agent_id });
    await reload();
  });

  const agentName = (id: number | null) => agents.data?.find((a) => a.id === id)?.name ?? `#${id}`;
  const target = (s: Schedule) => s.agent_id
    ? <span>agent <span className="mono">{agentName(s.agent_id)}</span></span>
    : <span className="mono">{String(s.task_template_json.assigned_provider_profile ?? "auto model")}</span>;
  const ready = f.name.trim() && cronCheck?.valid
    && (f.advanced || f.request.trim()) && (f.mode === "task" || f.agent_id);

  return (
    <>
      <PageHeader title="Scheduled Tasks"
        subtitle="Cron (UTC) schedules that run a saved agent or create a task. Each run goes through the harness routed for its model." />
      <ErrorNote error={error ?? act.error} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="card overflow-x-auto xl:col-span-2">
          <table className="w-full">
            <thead><tr>{["Name", "Runs", "Cron", "Enabled", "Last run", "Next run", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>{data?.map((s) => (
              <tr key={s.id}>
                <td className="td font-medium">{s.name}<div className="max-w-xs truncate text-xs text-muted">{String(s.task_template_json.title ?? s.task_template_json.description ?? "")}</div></td>
                <td className="td text-xs">{target(s)}</td>
                <td className="td mono whitespace-nowrap text-xs">{s.cron_expression}</td>
                <td className="td"><Toggle checked={s.enabled} onChange={(v) => act.run(async () => { await patch(`/schedules/${s.id}`, { enabled: v }); await reload(); })} /></td>
                <td className="td text-xs">{fmtTime(s.last_run_at)}</td>
                <td className="td text-xs">{fmtTime(s.next_run_at)}</td>
                <td className="td">
                  <div className="flex justify-end gap-2">
                    <button className="btn" onClick={() => act.run(async () => { await post(`/schedules/${s.id}/run`); await reload(); })}>Run now</button>
                    <ConfirmButton label="Delete" message={`Delete schedule ${s.name}? Tasks it already created are kept.`}
                      onConfirm={() => act.run(async () => { await del(`/schedules/${s.id}`); await reload(); })} />
                  </div>
                </td>
              </tr>))}</tbody>
          </table>
          {data && !data.length && <Empty>No scheduled tasks yet.</Empty>}
        </div>
        <Card title="New scheduled task">
          <div className="space-y-3">
            <Field label="Name"><input className="input" value={f.name} onChange={set("name")} placeholder="nightly-deps" /></Field>
            <div className="flex gap-1 rounded-md border border-line p-0.5 text-sm">
              {(["agent", "task"] as const).map((m) => (
                <button key={m} type="button" onClick={() => setF({ ...f, mode: m })}
                  className={`flex-1 rounded px-2 py-1 ${f.mode === m ? "bg-accent/15 text-accent" : "text-muted hover:text-ink"}`}>
                  {m === "agent" ? "Run an agent" : "One-off task"}
                </button>
              ))}
            </div>
            {f.mode === "agent" ? (
              <Field label="Agent">
                <select className="input" value={f.agent_id} onChange={set("agent_id")}>
                  <option value="">choose…</option>
                  {agents.data?.map((a) => <option key={a.id} value={a.id} disabled={!a.enabled}>{a.name}</option>)}
                </select>
                {agents.data && !agents.data.length && <div className="mt-1 text-xs text-muted">Create an agent on the Agents page first.</div>}
              </Field>
            ) : !f.advanced && (
              <>
                <Field label="Model">
                  <select className="input" value={f.model_profile} onChange={set("model_profile")}>
                    <option value="">automatic (flash-class default)</option>
                    {profiles.data?.filter((p) => p.enabled && ["worker", "agent"].includes(p.role)).map((p) =>
                      <option key={p.id} value={p.name}>{p.name} · {p.model_name}</option>)}
                  </select>
                </Field>
                <Field label="Project">
                  <select className="input" value={f.project_id} onChange={set("project_id")}>
                    <option value="">(none: answers only)</option>
                    {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </Field>
              </>
            )}
            {f.advanced ? (
              <Field label="Task template (JSON, TaskCreate fields)">
                <textarea className="input mono h-40 text-xs" value={f.template} onChange={set("template")} />
              </Field>
            ) : (
              <>
                <Field label="Title (optional)"><input className="input" value={f.title} onChange={set("title")} /></Field>
                <Field label="Request"><textarea className="input h-24" value={f.request} onChange={set("request")}
                  placeholder="List outdated dependencies and open a task for any with security advisories." /></Field>
              </>
            )}
            <Field label="Cron (min hour dom month dow, UTC)">
              <input className="input mono" value={f.cron} onChange={set("cron")} />
            </Field>
            <div className="flex flex-wrap gap-1">
              {PRESETS.map(([label, expr]) => (
                <button key={expr} type="button" className={`btn text-xs ${f.cron === expr ? "btn-primary" : ""}`} onClick={() => setF({ ...f, cron: expr })}>{label}</button>
              ))}
            </div>
            {cronCheck && (cronCheck.valid
              ? <div className="text-xs text-muted"><Badge value="valid" tone="ok" /> next: {cronCheck.next_runs?.map(fmtTime).join(" · ")}</div>
              : <div className="text-xs text-bad">{cronCheck.error}</div>)}
            {cronCheck?.warning && <div className="rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">{cronCheck.warning}</div>}
            <label className="flex items-center gap-2 text-xs text-muted">
              <input type="checkbox" checked={f.advanced}
                onChange={(e) => setF({ ...f, advanced: e.target.checked, template: e.target.checked ? JSON.stringify(buildTemplate({ ...f, advanced: false }), null, 2) : f.template })} />
              edit the raw template JSON
            </label>
            <button className="btn btn-primary" disabled={!ready || act.busy} onClick={create}>Create</button>
          </div>
        </Card>
      </div>
    </>
  );
}
