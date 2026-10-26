import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, post } from "../api/client";
import type { Project, Task } from "../api/types";
import { Badge, Card, ConfirmButton, Empty, ErrorNote, Field, PageHeader, Pre, lines } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

const STATUSES = ["", "draft", "planned", "queued", "in_progress", "review", "blocked", "accepted", "rejected", "archived"];

function CreateTask({ projects, onCreated }: { projects: Project[]; onCreated: () => void }) {
  const [f, setF] = useState({ title: "", project_id: "", kind: "coding", risk: "low", priority: "normal",
    tags: "", phase_number: "", phase_slug: "", description: "", dod: "", criteria: "", queue: false });
  const { busy, error, run } = useAction();
  const set = (k: string) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = () => run(async () => {
    await post("/tasks", {
      title: f.title, project_id: f.project_id ? Number(f.project_id) : null, kind: f.kind, risk: f.risk,
      priority: f.priority, tags: f.tags.split(",").map((t) => t.trim()).filter(Boolean),
      phase_number: f.phase_number ? Number(f.phase_number) : null, phase_slug: f.phase_slug || null,
      description: f.description, definition_of_done: lines(f.dod), acceptance_criteria: lines(f.criteria),
      queue: f.queue,
    });
    setF({ ...f, title: "", description: "", dod: "", criteria: "" });
    onCreated();
  });
  return (
    <Card title="Create task">
      <ErrorNote error={error} />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <div className="md:col-span-2"><Field label="Title"><input className="input" value={f.title} onChange={set("title")} /></Field></div>
        <Field label="Project">
          <select className="input" value={f.project_id} onChange={set("project_id")}>
            <option value="">(none — agent task)</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field label="Kind">
          <select className="input" value={f.kind} onChange={set("kind")}>
            {["coding", "agent", "review", "maintenance"].map((k) => <option key={k}>{k}</option>)}
          </select>
        </Field>
        <Field label="Risk"><select className="input" value={f.risk} onChange={set("risk")}>{["low", "medium", "high"].map((k) => <option key={k}>{k}</option>)}</select></Field>
        <Field label="Priority"><select className="input" value={f.priority} onChange={set("priority")}>{["low", "normal", "high"].map((k) => <option key={k}>{k}</option>)}</select></Field>
        <Field label="Phase # / slug">
          <div className="flex gap-2">
            <input className="input w-20" value={f.phase_number} onChange={set("phase_number")} placeholder="1" />
            <input className="input" value={f.phase_slug} onChange={set("phase_slug")} placeholder="scaffold-backend" />
          </div>
        </Field>
        <Field label="Tags (comma)"><input className="input" value={f.tags} onChange={set("tags")} placeholder="qwen, mock" /></Field>
        <div className="md:col-span-2"><Field label="Description / steps"><textarea className="input h-24" value={f.description} onChange={set("description")} /></Field></div>
        <Field label="Definition of Done (one per line)"><textarea className="input h-24" value={f.dod} onChange={set("dod")} /></Field>
        <Field label="Acceptance criteria (one per line)"><textarea className="input h-24" value={f.criteria} onChange={set("criteria")} /></Field>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button className="btn btn-primary" disabled={busy || !f.title.trim()} onClick={submit}>Create</button>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" checked={f.queue} onChange={(e) => setF({ ...f, queue: e.target.checked })} /> queue immediately
        </label>
      </div>
    </Card>
  );
}

export default function Tasks() {
  const [params] = useSearchParams();
  const [filters, setFilters] = useState({ status: params.get("status") ?? "", project_id: "", kind: "", risk: "", tag: "" });
  const query = useMemo(() => {
    const q = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => v && q.set(k, v));
    if (filters.status === "archived") q.set("include_archived", "true");
    return `/tasks?${q}`;
  }, [filters]);
  const { data: tasks, error, reload } = useApi<Task[]>(query, 5000);
  const { data: projects } = useApi<Project[]>("/projects");
  const [showCreate, setShowCreate] = useState(params.get("new") === "1");
  const [preview, setPreview] = useState<{ id: number; data: unknown } | null>(null);
  const act = useAction();
  const action = (t: Task, verb: string, body?: unknown) => act.run(async () => { await post(`/tasks/${t.id}/${verb}`, body); await reload(); });
  const dryRun = (t: Task) => act.run(async () => {
    const res = await api(`/tasks/${t.id}/dry-run`);
    setPreview({ id: t.id, data: res });
  });
  const projName = (id: number | null) => projects?.find((p) => p.id === id)?.name ?? "—";
  const fset = (k: string) => (e: { target: { value: string } }) => setFilters({ ...filters, [k]: e.target.value });

  return (
    <>
      <PageHeader title="Tasks" subtitle="Phase tasks move through the gated build loop as artifacts, not chat."
        actions={<button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>{showCreate ? "Close" : "New task"}</button>} />
      {showCreate && <div className="mb-4"><CreateTask projects={projects ?? []} onCreated={reload} /></div>}
      <ErrorNote error={error ?? act.error} />
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <select className="input" value={filters.status} onChange={fset("status")}>{STATUSES.map((s) => <option key={s} value={s}>{s || "any status"}</option>)}</select>
        <select className="input" value={filters.project_id} onChange={fset("project_id")}>
          <option value="">any project</option>{projects?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select className="input" value={filters.kind} onChange={fset("kind")}>{["", "coding", "agent", "review", "maintenance"].map((s) => <option key={s} value={s}>{s || "any kind"}</option>)}</select>
        <select className="input" value={filters.risk} onChange={fset("risk")}>{["", "low", "medium", "high"].map((s) => <option key={s} value={s}>{s || "any risk"}</option>)}</select>
        <input className="input" placeholder="tag" value={filters.tag} onChange={fset("tag")} />
      </div>
      <div className="card overflow-x-auto">
        <table className="w-full">
          <thead><tr>{["ID", "Title", "Project", "Status", "Kind", "Risk", "Tags", "Actions"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>
            {tasks?.map((t) => (
              <tr key={t.id} className="hover:bg-panel-2/50">
                <td className="td mono text-muted">{t.short_id}</td>
                <td className="td"><Link className="hover:text-accent" to={`/tasks/${t.id}`}>{t.title}</Link>
                  {t.branch_name && <div className="mono text-xs text-muted">{t.branch_name}</div>}</td>
                <td className="td">{projName(t.project_id)}</td>
                <td className="td"><Badge value={t.status} /></td>
                <td className="td">{t.kind}</td>
                <td className="td"><Badge value={t.risk} /></td>
                <td className="td text-xs text-muted">{t.tags.join(", ")}</td>
                <td className="td">
                  <div className="flex flex-wrap gap-1">
                    {["planned", "draft", "rejected"].includes(t.status) && <button className="btn" onClick={() => action(t, "queue")}>Queue</button>}
                    {["planned", "queued", "rejected"].includes(t.status) && <button className="btn btn-primary" onClick={() => action(t, "run")}>Run</button>}
                    <button className="btn" onClick={() => dryRun(t)}>Dry-run</button>
                    {["planned", "rejected", "blocked"].includes(t.status) && <button className="btn" onClick={() => action(t, "retry")}>Retry</button>}
                    {["queued", "in_progress"].includes(t.status) && <ConfirmButton label="Cancel" message={`Cancel ${t.short_id}? A running process is terminated.`} onConfirm={() => action(t, "cancel")} />}
                    {t.status === "blocked" && <button className="btn" onClick={() => action(t, "unblock", { reason: "unblocked from GUI" })}>Unblock</button>}
                    {["planned", "queued"].includes(t.status) && <button className="btn" onClick={() => { const reason = prompt("Why is this blocked?"); if (reason) void action(t, "block", { reason }); }}>Block</button>}
                    {["draft", "planned", "blocked", "rejected", "accepted"].includes(t.status) && <ConfirmButton label="Archive" message={`Archive ${t.short_id}? It disappears from default views.`} onConfirm={() => action(t, "archive")} />}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {tasks && !tasks.length && <Empty>No tasks match.</Empty>}
      </div>
      {preview && (
        <div className="mt-4"><Card title={`Dry-run preview (task ${preview.id})`} actions={<button className="btn" onClick={() => setPreview(null)}>Close</button>}>
          <Pre>{JSON.stringify(preview.data, null, 2)}</Pre>
        </Card></div>
      )}
    </>
  );
}
