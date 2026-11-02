import { useState } from "react";
import { Link } from "react-router-dom";
import { api, del, patch, post } from "../api/client";
import type { Adapter, Agent, AgentDetail, AgentOutputs, ModelProfile, Project, StandaloneRun, Task } from "../api/types";
import { Badge, Card, ConfirmButton, Empty, ErrorNote, Field, PageHeader, Toggle, fmtTime, lines } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

const BLANK = { name: "", description: "", instructions: "", model_profile: "", harness_adapter: "", project_id: "", tags: "" };

function AgentForm({ editing, profiles, adapters, projects, onSaved, onCancel }: {
  editing: Agent | null; profiles: ModelProfile[]; adapters: Adapter[]; projects: Project[];
  onSaved: (a: Agent) => void; onCancel: () => void;
}) {
  const [f, setF] = useState(editing ? {
    name: editing.name, description: editing.description, instructions: editing.instructions,
    model_profile: editing.model_profile ?? "", harness_adapter: editing.harness_adapter ?? "",
    project_id: editing.project_id ? String(editing.project_id) : "", tags: editing.tags.join(", "),
  } : BLANK);
  const act = useAction();
  const set = (k: keyof typeof BLANK) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const save = () => act.run(async () => {
    const body = {
      description: f.description, instructions: f.instructions,
      tags: f.tags.split(",").map((t) => t.trim()).filter(Boolean),
    };
    const a = editing
      ? await patch<Agent>(`/agents/${editing.id}`, {
          ...body, model_profile: f.model_profile, harness_adapter: f.harness_adapter,
          project_id: f.project_id ? Number(f.project_id) : 0,
        })
      : await post<Agent>("/agents", {
          ...body, name: f.name, model_profile: f.model_profile || null,
          harness_adapter: f.harness_adapter || null, project_id: f.project_id ? Number(f.project_id) : null,
        });
    if (!editing) setF(BLANK);
    onSaved(a);
  });
  const workerProfiles = profiles.filter((p) => p.enabled && ["worker", "agent"].includes(p.role));
  return (
    <Card title={editing ? `Edit ${editing.name}` : "New agent"}>
      <ErrorNote error={act.error} />
      <div className="space-y-3">
        {!editing && <Field label="Name"><input className="input" value={f.name} onChange={set("name")} placeholder="dependency-auditor" /></Field>}
        <Field label="Description"><input className="input" value={f.description} onChange={set("description")} /></Field>
        <Field label="Instructions (sent with every run)">
          <textarea className="input h-32" value={f.instructions} onChange={set("instructions")}
            placeholder="You keep dependencies current. Never bump a major version without a note in the handoff." />
        </Field>
        <Field label="Model">
          <select className="input" value={f.model_profile} onChange={set("model_profile")}>
            <option value="">automatic (default routing)</option>
            {workerProfiles.map((p) => <option key={p.id} value={p.name}>{p.name} · {p.model_name}</option>)}
          </select>
        </Field>
        <Field label="Harness">
          <select className="input" value={f.harness_adapter} onChange={set("harness_adapter")}>
            <option value="">automatic (best harness for the model)</option>
            {adapters.filter((a) => a.enabled).map((a) => (
              <option key={a.name} value={a.name}>{a.name}{a.detected ? "" : " (not installed)"}</option>
            ))}
          </select>
        </Field>
        <Field label="Project">
          <select className="input" value={f.project_id} onChange={set("project_id")}>
            <option value="">(none: answers only, can't edit code)</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field label="Tags (comma)"><input className="input" value={f.tags} onChange={set("tags")} /></Field>
        <div className="flex gap-2">
          <button className="btn btn-primary" disabled={act.busy || (!editing && !f.name.trim())} onClick={save}>
            {editing ? "Save" : "Create"}
          </button>
          {editing && <button className="btn" onClick={onCancel}>Cancel</button>}
        </div>
      </div>
    </Card>
  );
}

function OutputRow({ run, onChanged }: { run: StandaloneRun; onChanged: () => void }) {
  const [report, setReport] = useState<string | null>(null);
  const act = useAction();
  const toggle = () => act.run(async () => {
    setReport(report === null ? (await api<{ text: string }>(`/standalone-runs/${run.id}/report`)).text : null);
  });
  const tone = run.status === "completed" ? "ok" : run.status === "running" ? undefined : "warn";
  return (
    <li className="border-t border-line py-2 text-sm first:border-t-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge value={run.status} tone={tone} />
        <span className="text-xs text-muted">{fmtTime(run.started_at)}</span>
        <Badge value={run.access === "write" ? "can make changes" : "read and report"} />
        {run.usage.input_tokens ? <span className="text-xs text-muted">{run.usage.input_tokens.toLocaleString()} in · {(run.usage.output_tokens ?? 0).toLocaleString()} out</span> : null}
        <div className="ml-auto flex gap-2">
          {run.report_path && <button className="btn" disabled={act.busy} onClick={toggle}>{report === null ? "Report" : "Hide"}</button>}
          {run.chat_id && <Link className="btn" to={`/chat/${run.chat_id}`}>Session</Link>}
          {run.status === "running" && <button className="btn" disabled={act.busy} onClick={() => act.run(async () => { await post(`/standalone-runs/${run.id}/stop`); onChanged(); })}>Stop</button>}
        </div>
      </div>
      <ErrorNote error={act.error} />
      {run.summary && <p className="mt-1 line-clamp-2 text-muted">{run.summary}</p>}
      {run.error && <p className="mt-1 text-xs text-bad">{run.error}</p>}
      {run.blocked.length > 0 && <p className="mt-1 text-xs text-warn">{run.blocked.length} blocked action{run.blocked.length === 1 ? "" : "s"}</p>}
      {run.files.length > 0 && <p className="mono mt-1 text-xs text-muted">{run.files.join(" · ")}</p>}
      {report !== null && <pre className="mono mt-2 max-h-96 overflow-auto rounded-md border border-line p-3 text-xs whitespace-pre-wrap">{report}</pre>}
    </li>
  );
}

function Outputs({ id }: { id: number }) {
  const { data, error, reload } = useApi<AgentOutputs>(`/agents/${id}/outputs`, 5000);
  return (
    <>
      <h4 className="mt-5 mb-1 text-sm font-semibold">Outputs</h4>
      <ErrorNote error={error} />
      {data && <p className="mono mb-2 text-xs break-all text-muted">{data.folder}</p>}
      {data?.runs.length
        ? <ul>{data.runs.map((r) => <OutputRow key={r.id} run={r} onChanged={() => void reload()} />)}</ul>
        : <Empty>No standalone runs yet.</Empty>}
    </>
  );
}

function AgentPanel({ id, onChanged }: { id: number; onChanged: () => void }) {
  const { data, error, reload } = useApi<AgentDetail>(`/agents/${id}`, 10000);
  const [req, setReq] = useState({ request: "", dod: "", queue: true });
  const act = useAction();
  if (!data) return <ErrorNote error={error} />;
  const r = data.routing;
  const run = () => act.run(async () => {
    await post<Task>(`/agents/${id}/run`, { request: req.request, definition_of_done: lines(req.dod), queue: req.queue });
    setReq({ ...req, request: "", dod: "" });
    await reload();
    onChanged();
  });
  return (
    <Card title={data.name} actions={<Badge value={data.project_id ? "gated coding" : "answer only"} tone={data.project_id ? "ok" : undefined} />}>
      <ErrorNote error={error ?? act.error} />
      {data.description && <p className="mb-3 text-sm text-muted">{data.description}</p>}
      <dl className="mb-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted">Model</dt>
        <dd><span className="mono">{r.profile ?? "—"}</span>{r.model && <span className="text-muted"> · {r.model}</span>}{r.token_plan && <> <Badge value="Token Plan" /></>}
          <div className="text-xs text-muted">{r.profile_reason}</div></dd>
        <dt className="text-muted">Harness</dt>
        <dd><span className="mono">{r.harness ?? "—"}</span><div className="text-xs text-muted">{r.harness_reason}</div></dd>
      </dl>
      <div className="space-y-3">
        <Field label="Request"><textarea className="input h-24" value={req.request} onChange={(e) => setReq({ ...req, request: e.target.value })} /></Field>
        {data.project_id && (
          <Field label="Definition of Done (one per line, optional)">
            <textarea className="input h-16" value={req.dod} onChange={(e) => setReq({ ...req, dod: e.target.value })} />
          </Field>
        )}
        <div className="flex items-center gap-3">
          <button className="btn btn-primary" disabled={act.busy || !data.enabled || !req.request.trim()} onClick={run}>Run</button>
          <label className="flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" checked={req.queue} onChange={(e) => setReq({ ...req, queue: e.target.checked })} /> queue immediately
          </label>
          {!data.enabled && <span className="text-xs text-muted">agent is disabled</span>}
        </div>
      </div>
      <h4 className="mt-5 mb-2 text-sm font-semibold">Recent runs</h4>
      {data.recent_tasks.length ? (
        <ul className="space-y-1 text-sm">
          {data.recent_tasks.map((t) => (
            <li key={t.id} className="flex items-center gap-2">
              <Link className="mono text-accent" to={`/tasks/${t.id}`}>{t.short_id}</Link>
              <Badge value={t.status} />
              <span className="truncate text-muted">{t.title}</span>
            </li>
          ))}
        </ul>
      ) : <Empty>No runs yet.</Empty>}
      <Outputs id={id} />
    </Card>
  );
}

export default function Agents() {
  const agents = useApi<Agent[]>("/agents");
  const profiles = useApi<ModelProfile[]>("/model-profiles");
  const adapters = useApi<Adapter[]>("/adapters");
  const projects = useApi<Project[]>("/projects");
  const [selected, setSelected] = useState<number | null>(null);
  const [editing, setEditing] = useState<Agent | null>(null);
  const act = useAction();
  const projectName = (id: number | null) => projects.data?.find((p) => p.id === id)?.name ?? "—";

  return (
    <>
      <PageHeader title="Agents"
        subtitle="Saved agents with their own instructions and model. With a project, each run goes through the model's harness in an isolated worktree, then tests and Architect review." />
      <ErrorNote error={agents.error ?? act.error} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <div className="card overflow-x-auto">
            <table className="w-full">
              <thead><tr>{["Name", "Model", "Harness", "Project", "Enabled", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
              <tbody>{agents.data?.map((a) => (
                <tr key={a.id} className={selected === a.id ? "bg-accent/5" : ""}>
                  <td className="td font-medium">
                    <button className="text-left hover:text-accent" onClick={() => setSelected(a.id)}>{a.name}</button>
                    {a.description && <div className="text-xs text-muted">{a.description}</div>}
                  </td>
                  <td className="td mono text-xs">{a.model_profile ?? "auto"}</td>
                  <td className="td mono text-xs">{a.harness_adapter ?? "auto"}</td>
                  <td className="td text-xs">{projectName(a.project_id)}</td>
                  <td className="td"><Toggle checked={a.enabled} onChange={(v) => act.run(async () => { await patch(`/agents/${a.id}`, { enabled: v }); await agents.reload(); })} /></td>
                  <td className="td">
                    <div className="flex justify-end gap-2">
                      <button className="btn" onClick={() => setSelected(a.id)}>Run</button>
                      <button className="btn" onClick={() => setEditing(a)}>Edit</button>
                      <ConfirmButton label="Delete" message={`Delete agent ${a.name}? Its past tasks are kept.`}
                        onConfirm={() => act.run(async () => {
                          await del(`/agents/${a.id}`);
                          if (selected === a.id) setSelected(null);
                          await agents.reload();
                        })} />
                    </div>
                  </td>
                </tr>))}</tbody>
            </table>
            {agents.data && !agents.data.length && <Empty>No agents yet. Create one on the right.</Empty>}
          </div>
          {selected && <AgentPanel key={selected} id={selected} onChanged={agents.reload} />}
        </div>
        <AgentForm key={editing?.id ?? "new"} editing={editing}
          profiles={profiles.data ?? []} adapters={adapters.data ?? []} projects={projects.data ?? []}
          onCancel={() => setEditing(null)}
          onSaved={async (a) => { setEditing(null); setSelected(a.id); await agents.reload(); }} />
      </div>
    </>
  );
}
