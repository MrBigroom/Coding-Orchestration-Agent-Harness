import { useState } from "react";
import { patch, post } from "../api/client";
import type { Adapter } from "../api/types";
import { Badge, Card, ErrorNote, Field, PageHeader, Toggle } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";

function AdapterCard({ a, onSaved }: { a: Adapter; onSaved: () => void }) {
  const [command, setCommand] = useState(a.command);
  const [args, setArgs] = useState(JSON.stringify(a.args_template));
  const [env, setEnv] = useState(a.env_vars.join(", "));
  const [timeout, setTimeoutS] = useState(String(a.timeout_seconds));
  const act = useAction();
  const save = () => act.run(async () => {
    let parsed: unknown;
    try { parsed = JSON.parse(args); } catch { throw new Error("args must be a JSON list of strings"); }
    await patch(`/adapters/${a.name}`, { command, args_template_json: parsed, timeout_seconds: Number(timeout),
      env_vars_json: env.split(",").map((s) => s.trim()).filter(Boolean) });
    onSaved();
  });
  return (
    <Card title={<>{a.name} {a.experimental && <span className="ml-1"><Badge value="experimental" tone="warn" /></span>}</>}
      actions={<>
        {a.detected ? <Badge value="detected" tone="ok" /> : <Badge value="not found" tone="muted" />}
        <Toggle checked={a.enabled} label={a.enabled ? "enabled" : "disabled"} onChange={(v) => act.run(async () => { await patch(`/adapters/${a.name}`, { enabled: v }); onSaved(); })} />
      </>}>
      <ErrorNote error={act.error} />
      <p className="mb-3 text-sm text-muted">{a.notes}</p>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <Field label="Command"><input className="input mono text-xs" value={command} onChange={(e) => setCommand(e.target.value)} /></Field>
        <div className="md:col-span-3"><Field label="Args template (JSON list)"><input className="input mono text-xs" value={args} onChange={(e) => setArgs(e.target.value)} /></Field></div>
        <div className="md:col-span-3"><Field label="Env vars passed through (names only)"><input className="input mono text-xs" value={env} onChange={(e) => setEnv(e.target.value)} /></Field></div>
        <Field label="Timeout (s)"><input className="input" value={timeout} onChange={(e) => setTimeoutS(e.target.value)} /></Field>
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>binary: <span className="mono">{a.binary ?? "—"}</span> · verify manually: <span className="mono">{a.verify_hint}</span></span>
        <button className="btn btn-primary" disabled={act.busy} onClick={save}>Save template</button>
      </div>
    </Card>
  );
}

export default function Adapters() {
  const { data, error, reload } = useApi<Adapter[]>("/adapters");
  const act = useAction();
  return (
    <>
      <PageHeader title="Harness adapters" subtitle="Headless CLI adapters. Tokens: {cwd} {prompt_file} {prompt} {model} {branch} {artifacts_dir} {run_id} {handoff_path}; prefix ? to drop a token when empty."
        actions={<button className="btn" onClick={() => act.run(async () => { await post("/adapters/detect"); await reload(); })}>Detect / refresh</button>} />
      <ErrorNote error={error ?? act.error} />
      <div className="space-y-4">{data?.map((a) => <AdapterCard key={`${a.name}-${a.command}-${a.enabled}`} a={a} onSaved={reload} />)}</div>
    </>
  );
}
