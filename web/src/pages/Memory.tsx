import { useMemo, useRef, useState } from "react";
import { ApiError, api, patch, post } from "../api/client";
import type { MemoryEntry } from "../api/types";
import { Badge, Card, Empty, ErrorNote, Field, PageHeader } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

type BudgetRole = {
  sections_now: Record<string, number>;
  excluded_now: number[];
  warnings: string[];
};
type BudgetReport = {
  budgets: Record<string, number>;
  budget_enabled: boolean;
  roles: Record<string, BudgetRole>;
};
type Casualty = {
  id: number;
  bucket: string;
  scope: string;
  roles?: string[];
  self?: boolean;
  reason?: string;
};
type EvictionDetail = { action?: string; entry_id?: number | null; evicted?: Casualty[] };

// The 400 body carries the casualties as STRUCTURE in `detail` (DoD 7c). Never key the
// override flow on a substring of the message: rewording the prose must not break it.
function casualtyLines(detail: EvictionDetail | undefined): string[] {
  return (detail?.evicted ?? []).map((row) =>
    `#${row.id} (${row.bucket}/${row.scope}${row.self ? ", this row itself would not be injected" : `, roles ${(row.roles ?? []).join(",")}`})`,
  );
}

export default function Memory() {
  const [filters, setFilters] = useState({ status: "", category: "", project_slug: "", role: "" });
  const [q, setQ] = useState("");
  const path = useMemo(() => {
    if (q.trim()) return `/memory/search?q=${encodeURIComponent(q.trim())}`;
    const p = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => v && p.set(k, v));
    return `/memory?${p}`;
  }, [filters, q]);
  const { data, error, reload } = useApi<MemoryEntry[]>(path);
  const budget = useApi<BudgetReport>("/memory/budget");
  const act = useAction();
  const [edit, setEdit] = useState<{ id: number; content: string } | null>(null);
  const [form, setForm] = useState({ category: "global", scope: "preferences", project_slug: "", role: "", content: "" });
  const fileRef = useRef<HTMLInputElement>(null);
  const fset = (k: string) => (e: { target: { value: string } }) => setFilters({ ...filters, [k]: e.target.value });

  const exportAll = () => act.run(async () => {
    const data = await api("/memory/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "orchestrator-memory.json";
    a.click();
    URL.revokeObjectURL(a.href);
  });
  const importFile = (file: File) => act.run(async () => {
    const payload = JSON.parse(await file.text());
    const r = await post<{ imported: number }>("/memory/import", payload);
    alert(`Imported ${r.imported} entries.`);
    await reload();
  });
  const add = () => act.run(async () => {
    await post("/memory", { category: form.category, scope: form.scope, content: form.content,
      project_slug: form.project_slug || null, role: form.role || null, approved: false, source: "gui" });
    setForm({ ...form, content: "" });
    await reload();
  });
  // An approval that would evict an approved entry (or leave itself out of the injected
  // section) is refused (400) with the casualties in `detail.evicted`. Show that list and
  // offer the explicit override; never approve silently over the cap.
  const approve = (id: number) => act.run(async () => {
    const call = (override: boolean) => post(`/memory/${id}/approve${override ? "?override=true" : ""}`);
    try {
      await call(false);
    } catch (e) {
      const detail = e instanceof ApiError ? (e.detail as EvictionDetail | null) : null;
      const lines = casualtyLines(detail ?? undefined);
      if (!lines.length) throw e;
      const message = `Approving #${id} would demote:\n${lines.join("\n")}\n\nOverride and approve anyway? The demoted entries stay approved but are not injected.`;
      if (!window.confirm(message)) return;
      await call(true);
    }
    await reload();
    await budget.reload();
  });

  return (
    <>
      <PageHeader title="Shared memory" subtitle="Only approved entries are injected into prompts (as part of the cached prefix). Change memory at phase boundaries."
        actions={<>
          <button className="btn" onClick={exportAll}>Export</button>
          <button className="btn" onClick={() => fileRef.current?.click()}>Import</button>
          <input ref={fileRef} type="file" accept="application/json" className="hidden" onChange={(e) => e.target.files?.[0] && importFile(e.target.files[0])} />
        </>} />
      <ErrorNote error={error ?? act.error} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
            <input className="input md:col-span-1" placeholder="search…" value={q} onChange={(e) => setQ(e.target.value)} />
            <select className="input" value={filters.status} onChange={fset("status")}>{["", "pending", "approved", "rejected"].map((s) => <option key={s} value={s}>{s || "any status"}</option>)}</select>
            <select className="input" value={filters.category} onChange={fset("category")}>{["", "global", "project", "role"].map((s) => <option key={s} value={s}>{s || "any category"}</option>)}</select>
            <input className="input" placeholder="project slug" value={filters.project_slug} onChange={fset("project_slug")} />
            <select className="input" value={filters.role} onChange={fset("role")}>{["", "architect", "worker", "supervisor"].map((s) => <option key={s} value={s}>{s || "any role"}</option>)}</select>
          </div>
          <div className="card divide-y divide-line">
            {data?.map((m) => (
              <div key={m.id} className="flex items-start justify-between gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                    <span className="mono">#{m.id}</span><Badge value={m.category} tone="info" /><span>{m.scope}</span>
                    {m.project_slug && <span>· {m.project_slug}</span>}{m.role && <span>· {m.role}</span>}<span>· {m.source}</span>
                  </div>
                  {edit?.id === m.id ? (
                    <div className="flex gap-2">
                      <textarea className="input h-20" value={edit.content} onChange={(e) => setEdit({ id: m.id, content: e.target.value })} />
                      <div className="flex flex-col gap-1">
                        <button className="btn btn-primary" onClick={() => act.run(async () => { await patch(`/memory/${m.id}`, { content: edit.content }); setEdit(null); await reload(); })}>Save</button>
                        <button className="btn" onClick={() => setEdit(null)}>Cancel</button>
                      </div>
                    </div>
                  ) : <div className="text-sm">{m.content}</div>}
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-1">
                  <Badge value={m.approved ? "approved" : m.rejected ? "rejected" : "pending"} />
                  {!m.approved && <button className="btn btn-primary" onClick={() => approve(m.id)}>Approve</button>}
                  {!m.rejected && <button className="btn btn-danger" onClick={() => act.run(async () => { await post(`/memory/${m.id}/reject`); await reload(); })}>Reject</button>}
                  <button className="btn" onClick={() => setEdit({ id: m.id, content: m.content })}>Edit</button>
                </div>
              </div>
            ))}
            {data && !data.length && <Empty>No memory entries.</Empty>}
          </div>
        </div>
        <div className="space-y-4">
          <Card title="Injection caps">
            <p className="mb-3 text-xs text-muted">A section over its cap demotes entries; the casualties are named here so a favourite cannot silently stop being injected. Tags <code>pinned</code>/<code>durable</code> and <code>tier:1</code>..<code>tier:3</code> order the demotion. Approving over a cap is refused with the casualty list in the error detail, and offers an explicit override.</p>
            {budget.data && Object.entries(budget.data.roles).map(([role, p]) => (
              <div key={role} className="mb-3 text-sm">
                <div className="flex justify-between gap-2"><b>{role}</b><span className="text-xs text-muted">{Object.entries(p.sections_now).map(([k, v]) => `${k} ${v}/${budget.data?.budgets[k] ?? 0}`).join(" · ")}</span></div>
                <div className="text-xs text-muted">not injected: {p.excluded_now.length ? p.excluded_now.map((id) => `#${id}`).join(", ") : "none"}</div>
                {p.warnings.map((w) => <div key={w} className="text-xs text-warn">{w}</div>)}
              </div>
            ))}
            {budget.data && !budget.data.budget_enabled && <div className="text-xs text-warn">caps disabled: every approved entry is injected</div>}
          </Card>
          <Card title="Add entry">
            <div className="space-y-3">
              <Field label="Category"><select className="input" value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>{["global", "project", "role"].map((c) => <option key={c}>{c}</option>)}</select></Field>
              <Field label="Scope"><input className="input" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })} placeholder="environment | preferences | conventions | decisions | gotchas | worker" /></Field>
              {form.category === "project" && <Field label="Project slug"><input className="input" value={form.project_slug} onChange={(e) => setForm({ ...form, project_slug: e.target.value })} /></Field>}
              {form.category === "role" && <Field label="Role"><select className="input" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}><option value="">—</option>{["architect", "worker", "supervisor"].map((r) => <option key={r}>{r}</option>)}</select></Field>}
              <Field label="Content"><textarea className="input h-24" value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} /></Field>
              <button className="btn btn-primary" disabled={!form.content.trim()} onClick={add}>Add (pending approval)</button>
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
