import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
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
  time: "02:00", weekday: "1", monthday: "1", cron: "0 2 * * *", timezone: TIME_ZONE, advanced: false, template: "{}",
  run_mode: "pipeline" as "pipeline" | "standalone", access: "read" as "read" | "write", time_limit: "30" };

function buildCron(f: typeof BLANK): string {
  if (f.frequency === "cron") return f.cron;
  if (!f.time) return "";
  const [hour, minute] = f.time.split(":").map(Number);
  const days = { daily: "* * *", weekdays: "* * 1-5", weekly: `* * ${f.weekday}`, monthly: `${f.monthday} * *` };
  return `${minute} ${hour} ${days[f.frequency as keyof typeof days]}`;
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

const defaultTitle = (request: string) => request.split("\n")[0].slice(0, 80);

// `base` is the template being edited: fields the form doesn't show stay as they were.
function buildTemplate(f: typeof BLANK, base: Record<string, unknown> = {}): Record<string, unknown> {
  if (f.advanced) return JSON.parse(f.template);
  const { title: _title, assigned_provider_profile: _profile, ...kept } = base;
  if (f.mode === "agent") return { ...kept, description: f.request, ...(f.title ? { title: f.title } : {}) };
  const sameProject = f.project_id === String(base.project_id ?? "");
  return {
    ...kept,
    title: f.title || defaultTitle(f.request),
    description: f.request,
    project_id: f.project_id ? Number(f.project_id) : null,
    kind: sameProject && base.kind ? base.kind : f.project_id ? "coding" : "agent",
    ...(f.model_profile ? { assigned_provider_profile: f.model_profile } : {}),
  };
}

// Repeating schedules the picker can show reopen in it; anything else stays Custom cron.
function parseCron(cron: string): Partial<typeof BLANK> {
  const m = /^(\d{1,2}) (\d{1,2}) (\*|\d{1,2}) \* (\*|\d|1-5)$/.exec(cron);
  if (!m) return { frequency: "cron" };
  const [, minute, hour, dom, dow] = m;
  const time = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
  if (dom === "*" && dow === "*") return { frequency: "daily", time };
  if (dom === "*" && dow === "1-5") return { frequency: "weekdays", time };
  if (dom === "*") return { frequency: "weekly", time, weekday: String(Number(dow) % 7) };
  if (dow === "*") return { frequency: "monthly", time, monthday: String(Number(dom)) };
  return { frequency: "cron" };
}

// A moment as a datetime-local value in the given time zone.
function localInput(moment: string, zone: string): string {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(moment)).replace(" ", "T");
}

function fromSchedule(s: Schedule): typeof BLANK {
  const t = s.task_template_json;
  const request = String(t.description ?? "");
  const title = String(t.title ?? "");
  return {
    ...BLANK, ...(s.run_at ? {} : parseCron(s.cron_expression)),
    name: s.name, mode: s.agent_id ? "agent" : "task", agent_id: String(s.agent_id ?? ""),
    title: title === defaultTitle(request) ? "" : title, request,
    model_profile: String(t.assigned_provider_profile ?? ""), project_id: String(t.project_id ?? ""),
    when: s.run_at ? "once" : "repeat", run_at: s.run_at ? localInput(s.run_at, s.timezone) : "",
    cron: s.cron_expression || BLANK.cron, timezone: s.timezone,
    advanced: !request, template: JSON.stringify(t, null, 2),
    run_mode: s.run_mode, access: s.access, time_limit: String(s.time_limit_minutes),
  };
}

export default function Schedules() {
  const { data, error, reload } = useApi<Schedule[]>("/schedules", 10000);
  const agents = useApi<Agent[]>("/agents");
  const settings = useApi<Record<string, unknown>>("/settings");
  const profiles = useApi<ModelProfile[]>("/model-profiles");
  const projects = useApi<Project[]>("/projects");
  const [f, setF] = useState(BLANK);
  const [cronCheck, setCronCheck] = useState<CronCheck | null>(null);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const form = useRef<HTMLDivElement>(null);
  const act = useAction();
  const set = (k: keyof typeof BLANK) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const once = f.when === "once";
  const cron = buildCron(f);
  const standalone = f.mode === "agent" && f.run_mode === "standalone";
  const running = {
    run_mode: standalone ? "standalone" : "pipeline", access: f.access,
    time_limit_minutes: Number(f.time_limit) || 30,
  };
  const timing = once
    ? { run_at: f.run_at, timezone: f.timezone }
    : { cron_expression: cron, timezone: f.timezone };
  // An unchanged one-time schedule keeps its time, even a past one.
  const sameTime = once && !!editing?.run_at && f.timezone === editing.timezone
    && f.run_at === localInput(editing.run_at, editing.timezone);

  useEffect(() => {
    if (sameTime || (once ? !f.run_at : !cron)) { setCronCheck(null); return; }
    const t = setTimeout(() => {
      post<CronCheck>("/schedules/validate", {
        ...timing,
        agent_id: f.mode === "agent" && f.agent_id ? Number(f.agent_id) : null,
        model_profile: f.mode === "task" ? f.model_profile || null : null,
      }).then(setCronCheck).catch(() => setCronCheck(null));
    }, 300);
    return () => clearTimeout(t);
  }, [once, sameTime, f.run_at, cron, f.timezone, f.mode, f.agent_id, f.model_profile]);

  const startEdit = (s: Schedule) => {
    setEditing(s);
    setF(fromSchedule(s));
    form.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };
  const stopEdit = () => { setEditing(null); setF(BLANK); };

  const save = () => act.run(async () => {
    let template: Record<string, unknown>;
    try { template = buildTemplate(f, editing?.task_template_json); } catch { throw new Error("task template must be valid JSON"); }
    if (editing) {
      await patch(`/schedules/${editing.id}`, {
        name: f.name, task_template_json: template, timezone: f.timezone, ...running,
        agent_id: f.mode === "agent" ? Number(f.agent_id) : 0,
        ...(sameTime ? {} : timing),
        // A new time gives a finished one-time schedule another run.
        ...(finished(editing) && !sameTime ? { enabled: true } : {}),
      });
      stopEdit();
      await reload();
      return;
    }
    await post("/schedules", {
      name: f.name, ...timing, ...running, task_template_json: template, enabled: true,
      agent_id: f.mode === "agent" ? Number(f.agent_id) : null,
    });
    setF({ ...BLANK, mode: f.mode, agent_id: f.agent_id, run_mode: f.run_mode, access: f.access });
    await reload();
  });

  const agentName = (id: number | null) => agents.data?.find((a) => a.id === id)?.name ?? `#${id}`;
  const target = (s: Schedule) => s.agent_id
    ? <span>agent <span className="mono">{agentName(s.agent_id)}</span>{s.run_mode === "standalone" && <> <Badge value="standalone" /></>}</span>
    : <span className="mono">{String(s.task_template_json.assigned_provider_profile ?? "auto model")}</span>;
  const ready = f.name.trim() && (sameTime || cronCheck?.valid)
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
              <tr key={s.id} className={editing?.id === s.id ? "bg-accent/5" : ""}>
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
                    <button className="btn" onClick={() => startEdit(s)}>Edit</button>
                    <ConfirmButton label="Delete" message={`Delete schedule ${s.name}? Tasks it already created are kept.`}
                      onConfirm={() => act.run(async () => { await del(`/schedules/${s.id}`); if (editing?.id === s.id) stopEdit(); await reload(); })} />
                  </div>
                </td>
              </tr>))}</tbody>
          </table>
          {data && !data.length && <Empty>No scheduled tasks yet.</Empty>}
        </div>
        <Card title={editing ? "Edit schedule" : "New scheduled task"}>
          <div ref={form} className="space-y-3">
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
            {f.mode === "agent" && (
              <>
                <div className="flex gap-1 rounded-md border border-line p-0.5 text-sm">
                  {(["pipeline", "standalone"] as const).map((m) => (
                    <button key={m} type="button" onClick={() => setF({ ...f, run_mode: m })}
                      className={`flex-1 rounded px-2 py-1 ${f.run_mode === m ? "bg-accent/15 text-accent" : "text-muted hover:text-ink"}`}>
                      {m === "pipeline" ? "Pipeline" : "Standalone"}
                    </button>
                  ))}
                </div>
                {standalone ? (
                  <>
                    <div className="text-xs text-muted">Runs in its own harness session, with no task or review. Its report goes to the agent's Outputs.</div>
                    {settings.data && !settings.data.allow_unattended_sessions && (
                      <div className="rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">Standalone runs are off, so this schedule will be skipped. Turn them on in <Link className="underline" to="/settings">Settings</Link>.</div>
                    )}
                    <Field label="Access">
                      <select className="input" value={f.access} onChange={set("access")}>
                        <option value="read">Read and report</option>
                        <option value="write">Can make changes</option>
                      </select>
                    </Field>
                    {f.access === "write" && <div className="rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">Approves shell commands with nobody watching. Changes stay on a separate branch, but a worktree doesn't limit what the agent can reach.</div>}
                    <Field label="Time limit (minutes)">
                      <input type="number" min={1} max={1440} className="input" value={f.time_limit} onChange={set("time_limit")} />
                    </Field>
                  </>
                ) : <div className="text-xs text-muted">Creates a task, then goes through the queue, verification and review.</div>}
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
                <input type="datetime-local" className="input" min={localInput(new Date().toISOString(), f.timezone)} value={f.run_at} onChange={set("run_at")} />
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
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
              Times are in {f.timezone}.
              {f.timezone !== TIME_ZONE && <button type="button" className="text-accent hover:underline" onClick={() => setF({ ...f, timezone: TIME_ZONE })}>Use my time zone</button>}
            </div>
            {sameTime && editing && finished(editing) && <div className="text-xs text-muted">This schedule has finished. Choose a later time to run it again.</div>}
            {cronCheck && (cronCheck.valid
              ? <div className="text-xs text-muted"><Badge value="valid" tone="ok" /> {once ? "runs" : "next"}: {cronCheck.next_runs?.map(fmtTime).join(" · ")}</div>
              : <div className="text-xs text-bad">{cronCheck.error}</div>)}
            {cronCheck?.warning && <div className="rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">{cronCheck.warning}</div>}
            <label className="flex items-center gap-2 text-xs text-muted">
              <input type="checkbox" checked={f.advanced}
                onChange={(e) => setF({ ...f, advanced: e.target.checked, template: e.target.checked ? JSON.stringify(buildTemplate({ ...f, advanced: false }, editing?.task_template_json), null, 2) : f.template })} />
              edit the raw template JSON
            </label>
            <div className="flex gap-2">
              <button className="btn btn-primary" disabled={!ready || act.busy} onClick={save}>{editing ? "Save" : "Create"}</button>
              {editing && <button className="btn" onClick={stopEdit}>Cancel</button>}
            </div>
          </div>
        </Card>
      </div>
    </>
  );
}
