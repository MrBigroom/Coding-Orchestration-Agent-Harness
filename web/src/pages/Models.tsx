import { useState } from "react";
import { patch, post } from "../api/client";
import type { ModelProfile, ProviderStatus } from "../api/types";
import { Badge, Card, ErrorNote, PageHeader, Toggle } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

export default function Models() {
  const providers = useApi<ProviderStatus[]>("/providers");
  const profiles = useApi<ModelProfile[]>("/model-profiles");
  const act = useAction();
  const [editing, setEditing] = useState<Record<number, string>>({});
  const [windows, setWindows] = useState<Record<number, string>>({});
  const provName = (id: number) => providers.data?.find((p) => p.id === id)?.name ?? "?";
  const [checks, setChecks] = useState<Record<number, string>>({});
  const [adding, setAdding] = useState(false);

  return (
    <>
      <PageHeader title="Models & Providers" subtitle="API keys live in your environment/.env only. The GUI shows whether a key is set, never its value." />
      <ErrorNote error={providers.error ?? profiles.error ?? act.error} />
      <Card title="Providers" className="mb-4">
        <div className="overflow-x-auto"><table className="w-full">
          <thead><tr>{["Name", "Kind", "Base URL", "Key env", "Key set", "Status", "Enabled", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{providers.data?.map((p) => (
            <tr key={p.id}>
              <td className="td font-medium">{p.name}<div className="max-w-xs text-xs text-muted">{p.notes}</div></td>
              <td className="td">{p.kind}</td>
              <td className="td min-w-56 max-w-xs text-xs"><BaseUrl value={p.base_url} /></td>
              <td className="td mono text-xs">{p.api_key_env ?? "—"}</td>
              <td className="td">{p.api_key_env ? <Badge value={p.api_key_set ? "set" : "missing"} tone={p.api_key_set ? "ok" : "warn"} /> : "—"}</td>
              <td className="td min-w-40">{p.configured ? <Badge value="ready" tone="ok" /> : <span className="text-xs text-warn">{p.problems.join("; ")}</span>}</td>
              <td className="td"><Toggle checked={p.enabled} onChange={(v) => act.run(async () => { await patch(`/providers/${p.id}`, { enabled: v }); await providers.reload(); })} /></td>
              <td className="td"><button className="btn" onClick={() => act.run(async () => {
                const r = await post<{ ok: boolean; problems: string[] }>(`/providers/${p.id}/test`);
                setChecks({ ...checks, [p.id]: r.ok ? "config OK (no network call made)" : r.problems.join("; ") });
              })}>Check</button>{checks[p.id] && <div className="mt-1 text-xs text-muted">{checks[p.id]}</div>}</td>
            </tr>))}</tbody>
        </table></div>
      </Card>
      <Card title="Model profiles" actions={<button className="btn" onClick={() => setAdding(!adding)}>{adding ? "Cancel" : "Add model"}</button>}>
        {adding && providers.data && <AddModel providers={providers.data} onAdded={async () => { setAdding(false); await profiles.reload(); }} />}
        <p className="mb-3 text-xs text-muted">Model IDs are editable placeholders. Claude 5.x models ignore temperature; use effort instead. Context is the model's window in tokens, shown in Chat; leave it empty to use the built-in figure.</p>
        <div className="overflow-x-auto"><table className="w-full">
          <thead><tr>{["Name", "Role", "Provider", "Model", "Effort", "Max tokens", "Context", "In Chat", "Enabled"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{profiles.data?.map((m) => (
            <tr key={m.id}>
              <td className="td font-medium">{m.name}<div className="max-w-xs text-xs text-muted">{m.notes}</div></td>
              <td className="td"><Badge value={m.role} tone="info" /></td>
              <td className="td">{provName(m.provider_id)}</td>
              <td className="td">
                <input className="input mono w-48 text-xs" value={editing[m.id] ?? m.model_name}
                  onChange={(e) => setEditing({ ...editing, [m.id]: e.target.value })}
                  onBlur={() => editing[m.id] !== undefined && editing[m.id] !== m.model_name &&
                    act.run(async () => { await patch(`/model-profiles/${m.id}`, { model_name: editing[m.id] }); await profiles.reload(); })} />
              </td>
              <td className="td">
                <select className="input w-auto text-xs" value={m.effort ?? ""} onChange={(e) => act.run(async () => { await patch(`/model-profiles/${m.id}`, { effort: e.target.value || null }); await profiles.reload(); })}>
                  {["", "low", "medium", "high", "xhigh", "max"].map((x) => <option key={x} value={x}>{x || "—"}</option>)}
                </select>
              </td>
              <td className="td">{m.max_tokens}</td>
              <td className="td">
                <input className="input mono w-28 text-xs" inputMode="numeric" aria-label={`Context window for ${m.name}`}
                  placeholder={m.builtin_context_window?.toLocaleString() ?? "unknown"}
                  value={windows[m.id] ?? m.context_window?.toString() ?? ""}
                  onChange={(e) => setWindows({ ...windows, [m.id]: e.target.value.replace(/[^0-9]/g, "") })}
                  onBlur={() => {
                    const raw = windows[m.id];
                    if (raw === undefined) return;
                    const value = raw ? Number(raw) : null;
                    if (value === m.context_window) return;
                    void act.run(async () => { await patch(`/model-profiles/${m.id}`, { context_window: value }); await profiles.reload(); });
                  }} />
              </td>
              <td className="td text-xs">{m.chat_harnesses.length ? <span className="mono">{m.chat_harnesses.join(", ")}</span> : <span className="text-muted" title="No harness route matches this model ID, so Chat doesn't offer it.">—</span>}</td>
              <td className="td"><Toggle checked={m.enabled} onChange={(v) => act.run(async () => { await patch(`/model-profiles/${m.id}`, { enabled: v }); await profiles.reload(); })} /></td>
            </tr>))}</tbody>
        </table></div>
      </Card>
    </>
  );
}

const ROLES = ["worker", "agent", "supervisor", "architect"];

/** Creates a model profile. Chat offers it in the harnesses its model ID routes to. */
function AddModel({ providers, onAdded }: { providers: ProviderStatus[]; onAdded: () => Promise<void> }) {
  const act = useAction();
  const [form, setForm] = useState({ name: "", model_name: "", role: "worker", provider_id: providers.find((p) => p.enabled && p.kind !== "mock")?.id ?? providers[0]?.id ?? 0, effort: "", context_window: "", notes: "" });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const model = form.model_name.trim();
  const name = form.name.trim() || (model ? `${form.role}-${model}` : "");
  const submit = () => act.run(async () => {
    await post("/model-profiles", {
      name, model_name: model, role: form.role, provider_id: Number(form.provider_id),
      effort: form.effort || null, context_window: form.context_window ? Number(form.context_window) : null, notes: form.notes.trim() || null,
    });
    await onAdded();
  });
  return (
    <form className="mb-4 rounded-lg border border-line p-4" onSubmit={(e) => { e.preventDefault(); if (model) void submit(); }}>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-xs text-muted">Model ID<input className="input mono mt-1 text-xs" required autoFocus placeholder="e.g. qwen4-max-preview" value={form.model_name} onChange={set("model_name")} /></label>
        <label className="text-xs text-muted">Provider<select className="input mt-1 text-xs" value={form.provider_id} onChange={set("provider_id")}>
          {providers.map((p) => <option key={p.id} value={p.id}>{p.name}{p.enabled ? "" : " (disabled)"}</option>)}
        </select></label>
        <label className="text-xs text-muted">Role<select className="input mt-1 text-xs" value={form.role} onChange={set("role")}>
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select></label>
        <label className="text-xs text-muted">Profile name<input className="input mono mt-1 text-xs" placeholder={name || "defaults to role-model"} value={form.name} onChange={set("name")} /></label>
        <label className="text-xs text-muted">Effort<select className="input mt-1 text-xs" value={form.effort} onChange={set("effort")}>
          {["", "low", "medium", "high", "xhigh", "max"].map((x) => <option key={x} value={x}>{x || "—"}</option>)}
        </select></label>
        <label className="text-xs text-muted">Context window (tokens)<input className="input mono mt-1 text-xs" inputMode="numeric" placeholder="optional" value={form.context_window} onChange={(e) => setForm({ ...form, context_window: e.target.value.replace(/[^0-9]/g, "") })} /></label>
        <label className="text-xs text-muted sm:col-span-2 lg:col-span-3">Notes<input className="input mt-1 text-xs" placeholder="optional" value={form.notes} onChange={set("notes")} /></label>
      </div>
      <ErrorNote error={act.error} />
      <div className="mt-3 flex items-center gap-3">
        <button className="btn btn-primary" type="submit" disabled={!model || act.busy}>Add model</button>
        <span className="text-xs text-muted">Chat lists it under harnesses routed for its model ID (e.g. qwen… → Pi, Qwen Code).</span>
      </div>
    </form>
  );
}

/** `env:VAR|https://default` means the VAR environment variable, else the default URL. */
function BaseUrl({ value }: { value: string | null }) {
  if (!value) return <span className="text-muted">SDK default</span>;
  if (!value.startsWith("env:")) return <span className="mono break-all">{value}</span>;
  const [name, fallback] = value.slice(4).split("|", 2);
  return (
    <div className="space-y-0.5">
      <div><span className="text-muted">from </span><span className="mono break-all">{name}</span></div>
      <div className="mono break-all text-muted">{fallback ? <>else {fallback}</> : "else SDK default"}</div>
    </div>
  );
}
