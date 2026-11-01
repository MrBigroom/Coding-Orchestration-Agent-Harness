import { useEffect, useState } from "react";
import { del, patch, post } from "../api/client";
import type { Agent, ModelProfile, Project, Schedule } from "../api/types";
import { Badge, Card, ConfirmButton, Empty, ErrorNote, Field, PageHeader, Toggle, fmtTime } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

type CronCheck = { valid: boolean; next_runs?: string[]; error?: string; warning?: string | null };

const TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
const FREQUENCIES = [["daily", "Every day"], ["weekdays", "Weekdays"], ["weekly", "Weekly"], ["monthly", "Monthly"], ["cron", "Custom cron"]];
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

const BLANK = { name: "", mode: "agent" as "agent" | "task", agent_id: "", title: "", request: "",
  model_profile: "", project_id: "", when: "repeat" as "once" | "repeat", run_at: "", frequency: "daily",
  time: "02:00", weekday: "1", monthday: "1", cron: "0 2 * * *", advanced: false, template: "{}" };

function buildCron(f: typeof BLANK): string {
  if (f.frequency === "cron") return f.cron;
  if (!f.time) return "";
  const [hour, minute] = f.time.split(":").map(Number);
  const days = { daily: "* * *", weekdays: "* * 1-5", weekly: `* * ${f.weekday}`, monthly: `${f.monthday} * *` };
  return `${minute} ${hour} ${days[f.frequency as keyof typeof days]}`;
}

// Now, in the form a datetime-local input uses.
function localNow(): string {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function whenOf(s: Schedule): string {
  const zone = s.timezone === "UTC" ? " UTC" : "";
  return s.run_at ? "Once" : `${s.cron_expression}${zone}`;
}

// A one-time schedule can't be turned back on after its time has passed.
function finished(s: Schedule): string | null {
  if (!s.run_at || s.enabled || new Date(s.run_at) > new Date()) return null;
  return s.last_run_at && new Date(s.last_run_at) >= new Date(s.run_at) ? "done" : "not run";
}

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
  const once = f.when === "once";
  const cron = buildCron(f);
  const timing = once
    ? { run_at: f.run_at, timezone: TIME_ZONE }
    : { cron_expression: cron, timezone: TIME_ZONE };

  useEffect(() => {
    if (once ? !f.run_at : !cron) { setCronCheck(null); return; }
    const t = setTimeout(() => {
      post<CronCheck>("/schedules/validate", {
        ...timing,
        agent_id: f.mode === "agent" && f.agent_id ? Number(f.agent_id) : null,
        model_profile: f.mode === "task" ? f.model_profile || null : null,
      }).then(setCronCheck).catch(() => setCronCheck(null));
    }, 300);
    return () => clearTimeout(t);
  }, [once, f.run_at, cron, f.mode, f.agent_id, f.model_profile]);

  const create = () => act.run(async () => {
    let template: Record<string, unknown>;
    try { template = buildTemplate(f); } catch { throw new Error("task template must be valid JSON"); }
    await post("/schedules", {
      name: f.name, ...timing, task_template_json: template, enabled: true,
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
        subtitle="Run a saved agent or create a task, once or on repeat. Each run goes through the harness routed for its model." />
      <ErrorNote error={error ?? act.error} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="card overflow-x-auto xl:col-span-2">
          <table className="w-full">
            <thead><tr>{["Name", "Runs", "When", "Enabled", "Last run", "Next run", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>{data?.map((s) => (
              <tr key={s.id}>
                <td className="td font-medium">{s.name}<div className="max-w-xs truncate text-xs text-muted">{String(s.task_template_json.title ?? s.task_template_json.description ?? "")}</div></td>
                <td className="td text-xs">{target(s)}</td>
                <td className={`td whitespace-nowrap text-xs ${s.run_at ? "" : "mono"}`} title={s.run_at ? `Runs ${fmtTime(s.run_at)}` : s.timezone}>{whenOf(s)}</td>
                <td className="td">{finished(s)
                  ? <Badge value={finished(s) as string} />
                  : <Toggle checked={s.enabled} onChange={(v) => act.run(async () => { await patch(`/schedules/${s.id}`, { enabled: v }); await reload(); })} />}</td>
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
                  {m === "agent" ? "Run an agent" : "Run a task"}
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
            <div className="flex gap-1 rounded-md border border-line p-0.5 text-sm">
              {(["repeat", "once"] as const).map((w) => (
                <button key={w} type="button" onClick={() => setF({ ...f, when: w })}
                  className={`flex-1 rounded px-2 py-1 ${f.when === w ? "bg-accent/15 text-accent" : "text-muted hover:text-ink"}`}>
                  {w === "repeat" ? "Repeats" : "Once"}
                </button>
              ))}
            </div>
            {once ? (
              <Field label="Date and time">
                <input type="datetime-local" className="input" min={localNow()} value={f.run_at} onChange={set("run_at")} />
              </Field>
            ) : (
              <>
                <Field label="How often">
                  <select className="input" value={f.frequency} onChange={set("frequency")}>
                    {FREQUENCIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </select>
                </Field>
                {f.frequency === "weekly" && (
                  <Field label="Day">
                    <select className="input" value={f.weekday} onChange={set("weekday")}>
                      {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
                    </select>
                  </Field>
                )}
                {f.frequency === "monthly" && (
                  <Field label="Day of the month">
                    <select className="input" value={f.monthday} onChange={set("monthday")}>
                      {Array.from({ length: 31 }, (_, i) => <option key={i} value={i + 1}>{i + 1}</option>)}
                    </select>
                    {Number(f.monthday) > 28 && <div className="mt-1 text-xs text-muted">Skipped in months without this day.</div>}
                  </Field>
                )}
                {f.frequency === "cron" ? (
                  <Field label="Cron (min hour dom month dow)">
                    <input className="input mono" value={f.cron} onChange={set("cron")} />
                  </Field>
                ) : (
                  <Field label="Time">
                    <input type="time" className="input" value={f.time} onChange={set("time")} />
                  </Field>
                )}
              </>
            )}
            <div className="text-xs text-muted">Times are in {TIME_ZONE}.</div>
            {cronCheck && (cronCheck.valid
              ? <div className="text-xs text-muted"><Badge value="valid" tone="ok" /> {once ? "runs" : "next"}: {cronCheck.next_runs?.map(fmtTime).join(" · ")}</div>
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
