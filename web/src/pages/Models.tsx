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
  const provName = (id: number) => providers.data?.find((p) => p.id === id)?.name ?? "?";
  const [checks, setChecks] = useState<Record<number, string>>({});

  return (
    <>
      <PageHeader title="Models & Providers" subtitle="API keys live in your environment/.env only. The GUI shows whether a key is set, never its value." />
      <ErrorNote error={providers.error ?? profiles.error ?? act.error} />
      <Card title="Providers" className="mb-4">
        <table className="w-full">
          <thead><tr>{["Name", "Kind", "Base URL", "Key env", "Key set", "Status", "Enabled", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{providers.data?.map((p) => (
            <tr key={p.id}>
              <td className="td font-medium">{p.name}<div className="max-w-xs text-xs text-muted">{p.notes}</div></td>
              <td className="td">{p.kind}</td>
              <td className="td mono max-w-xs break-all text-xs">{p.base_url ?? "SDK default"}</td>
              <td className="td mono text-xs">{p.api_key_env ?? "—"}</td>
              <td className="td">{p.api_key_env ? <Badge value={p.api_key_set ? "set" : "missing"} tone={p.api_key_set ? "ok" : "warn"} /> : "—"}</td>
              <td className="td">{p.configured ? <Badge value="ready" tone="ok" /> : <span className="text-xs text-warn">{p.problems.join("; ")}</span>}</td>
              <td className="td"><Toggle checked={p.enabled} onChange={(v) => act.run(async () => { await patch(`/providers/${p.id}`, { enabled: v }); await providers.reload(); })} /></td>
              <td className="td"><button className="btn" onClick={() => act.run(async () => {
                const r = await post<{ ok: boolean; problems: string[] }>(`/providers/${p.id}/test`);
                setChecks({ ...checks, [p.id]: r.ok ? "config OK (no network call made)" : r.problems.join("; ") });
              })}>Check</button>{checks[p.id] && <div className="mt-1 text-xs text-muted">{checks[p.id]}</div>}</td>
            </tr>))}</tbody>
        </table>
      </Card>
      <Card title="Model profiles">
        <p className="mb-3 text-xs text-muted">Model IDs are editable placeholders. Claude 5.x models ignore temperature; use effort instead.</p>
        <table className="w-full">
          <thead><tr>{["Name", "Role", "Provider", "Model", "Effort", "Max tokens", "Enabled"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
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
              <td className="td"><Toggle checked={m.enabled} onChange={(v) => act.run(async () => { await patch(`/model-profiles/${m.id}`, { enabled: v }); await profiles.reload(); })} /></td>
            </tr>))}</tbody>
        </table>
      </Card>
    </>
  );
}
