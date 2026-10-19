import { useState } from "react";
import { api, patch, post } from "../api/client";
import type { Project } from "../api/types";
import { Badge, Card, Empty, ErrorNote, Field, PageHeader, Pre } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

type Doctor = { ok: boolean; checks: { name: string; ok: boolean; detail: string }[] };
const EMPTY = { name: "", path: "", base_branch: "", test_command: "", lint_command: "", build_command: "", allowed_commands: "", notes: "" };

export default function Projects() {
  const { data: projects, error, reload } = useApi<Project[]>("/projects");
  const [form, setForm] = useState(EMPTY);
  const [editing, setEditing] = useState<number | null>(null);
  const [doctor, setDoctor] = useState<Record<number, Doctor>>({});
  const [output, setOutput] = useState<string | null>(null);
  const act = useAction();
  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const body = () => ({
    path: form.path, base_branch: form.base_branch || null, test_command: form.test_command || null,
    lint_command: form.lint_command || null, build_command: form.build_command || null,
    allowed_commands: form.allowed_commands.split(",").map((s) => s.trim()).filter(Boolean), notes: form.notes || null,
  });
  const save = () => act.run(async () => {
    if (editing) await patch(`/projects/${editing}`, body());
    else await post("/projects", { name: form.name, ...body() });
    setForm(EMPTY);
    setEditing(null);
    await reload();
  });
  const edit = (p: Project) => {
    setEditing(p.id);
    setForm({ name: p.name, path: p.path, base_branch: p.base_branch, test_command: p.test_command ?? "",
      lint_command: p.lint_command ?? "", build_command: p.build_command ?? "",
      allowed_commands: p.allowed_commands.join(", "), notes: p.notes ?? "" });
  };
  const runDoctor = (p: Project) => act.run(async () => setDoctor({ ...doctor, [p.id]: await api<Doctor>(`/projects/${p.id}/doctor`) }));
  const runCmd = (p: Project, label: string) => act.run(async () => {
    const r = await post<{ command: string; exit_code: number; output: string }>(`/projects/${p.id}/commands/${label}`);
    setOutput(`$ ${r.command}\n(exit ${r.exit_code})\n\n${r.output}`);
  });

  return (
    <>
      <PageHeader title="Projects" subtitle="Git repositories the orchestrator may create phase worktrees in." />
      <ErrorNote error={error ?? act.error} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          {projects?.map((p) => (
            <Card key={p.id} title={<>{p.name} <span className="ml-2 text-xs text-muted">{p.slug}</span></>}
              actions={<>
                <button className="btn" onClick={() => runDoctor(p)}>Doctor</button>
                <button className="btn" onClick={() => edit(p)}>Edit</button>
                {p.test_command && <button className="btn" onClick={() => runCmd(p, "test")}>Run tests</button>}
              </>}>
              <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
                <dt className="text-muted">Path</dt><dd className="mono text-xs">{p.path}</dd>
                <dt className="text-muted">Base branch</dt><dd>{p.base_branch}</dd>
                <dt className="text-muted">Test</dt><dd className="mono text-xs">{p.test_command ?? "—"}</dd>
                <dt className="text-muted">Lint</dt><dd className="mono text-xs">{p.lint_command ?? "—"}</dd>
                <dt className="text-muted">Build</dt><dd className="mono text-xs">{p.build_command ?? "—"}</dd>
                <dt className="text-muted">Allowed commands</dt><dd className="mono text-xs">{p.allowed_commands.join(", ") || "—"}</dd>
              </dl>
              {doctor[p.id] && (
                <ul className="mt-3 space-y-1 border-t border-line pt-3 text-sm">
                  {doctor[p.id].checks.map((c) => (
                    <li key={c.name} className="flex gap-2"><Badge value={c.ok ? "pass" : "fail"} /> {c.name}{c.detail && <span className="text-muted">— {c.detail}</span>}</li>
                  ))}
                </ul>
              )}
            </Card>
          ))}
          {projects && !projects.length && <Card><Empty>No projects yet. Add one, or run <code className="mono">orchestrator demo</code>.</Empty></Card>}
          {output && <Card title="Command output" actions={<button className="btn" onClick={() => setOutput(null)}>Close</button>}><Pre>{output}</Pre></Card>}
        </div>
        <Card title={editing ? "Edit project" : "Add project"}>
          <div className="space-y-3">
            {!editing && <Field label="Name"><input className="input" value={form.name} onChange={set("name")} /></Field>}
            <Field label="Path (git repo)"><input className="input" value={form.path} onChange={set("path")} placeholder="/home/you/code/app" /></Field>
            <Field label="Base branch"><input className="input" value={form.base_branch} onChange={set("base_branch")} placeholder="auto-detect" /></Field>
            <Field label="Test command"><input className="input" value={form.test_command} onChange={set("test_command")} placeholder="pytest -q" /></Field>
            <Field label="Lint command"><input className="input" value={form.lint_command} onChange={set("lint_command")} placeholder="ruff check ." /></Field>
            <Field label="Build command"><input className="input" value={form.build_command} onChange={set("build_command")} /></Field>
            <Field label="Allowed commands (comma)"><input className="input" value={form.allowed_commands} onChange={set("allowed_commands")} /></Field>
            <Field label="Notes"><textarea className="input h-16" value={form.notes} onChange={set("notes")} /></Field>
            <p className="text-xs text-muted">Commands run without a shell: no pipes, &&, or redirects. Wrap complex steps in a script.</p>
            <div className="flex gap-2">
              <button className="btn btn-primary" disabled={act.busy || !form.path || (!editing && !form.name)} onClick={save}>{editing ? "Save" : "Add"}</button>
              {editing && <button className="btn" onClick={() => { setEditing(null); setForm(EMPTY); }}>Cancel</button>}
            </div>
          </div>
        </Card>
      </div>
    </>
  );
}
