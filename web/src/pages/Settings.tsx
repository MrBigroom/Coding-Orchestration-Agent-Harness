import { patch } from "../api/client";
import { getToken, setToken } from "../api/client";
import { Card, ErrorNote, PageHeader, ThemeSwitch, Toggle } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";
import { useState } from "react";

type S = Record<string, string | number | boolean>;

const BOOLS: [string, string][] = [
  ["merge_enabled", "Merge gate enabled (allows --no-ff merges of accepted phase branches)"],
  ["auto_merge_enabled", "Auto-merge after APPROVED + live verification (requires merge gate)"],
  ["supervisor_microfixes_enabled", "Allow Supervisor micro-fixes (otherwise code changes go back to the Worker)"],
  ["scheduler_enabled", "Scheduler enabled (takes effect on restart)"],
  ["worker_enabled", "Background queue worker enabled"],
  ["allow_gui_commands", "Allow running project test/lint/build commands from the GUI"],
];
const NUMS: [string, string][] = [
  ["max_parallel_runs", "Max parallel runs"],
  ["max_parallel_runs_per_project", "Max parallel runs per project"],
  ["max_review_rounds", "Max review rounds before NEEDS_HUMAN"],
];

export default function Settings() {
  const { data, error, setData } = useApi<S>("/settings");
  const act = useAction();
  const [token, setTok] = useState(getToken());
  const update = (key: string, value: unknown) => act.run(async () => setData(await patch<S>("/settings", { values: { [key]: value } })));
  return (
    <>
      <PageHeader title="Settings" subtitle="Defaults come from ORCHESTRATOR_* env vars; changes here are stored in SQLite." />
      <ErrorNote error={error ?? act.error} />
      {data && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card title="Gates & execution">
            <div className="space-y-3">
              {BOOLS.map(([k, label]) => (
                <div key={k} className="flex items-center justify-between gap-4">
                  <span className="text-sm">{label}</span>
                  <Toggle checked={Boolean(data[k])} onChange={(v) => update(k, v)} />
                </div>
              ))}
            </div>
          </Card>
          <Card title="Limits">
            <div className="space-y-3">
              {NUMS.map(([k, label]) => (
                <div key={k} className="flex items-center justify-between gap-4">
                  <span className="text-sm">{label}</span>
                  <input className="input w-24" type="number" min={1} defaultValue={Number(data[k])}
                    onBlur={(e) => Number(e.target.value) !== data[k] && update(k, Number(e.target.value))} />
                </div>
              ))}
            </div>
          </Card>
          <Card title="Appearance">
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm">Theme <span className="block text-xs text-muted">Saved in this browser. System follows your OS setting.</span></span>
              <ThemeSwitch full />
            </div>
          </Card>
          <Card title="Server">
            <dl className="grid grid-cols-2 gap-y-2 text-sm">
              <dt className="text-muted">Bind</dt><dd className="mono">{String(data.bind_host)}:{String(data.bind_port)}</dd>
              <dt className="text-muted">Token auth</dt><dd>{data.token_auth_enabled ? "enabled (ORCHESTRATOR_TOKEN)" : "disabled"}</dd>
            </dl>
          </Card>
          <Card title="This browser's token">
            <p className="mb-2 text-xs text-muted">Stored in this browser only; sent as X-Orchestrator-Token.</p>
            <div className="flex gap-2">
              <input className="input" type="password" value={token} onChange={(e) => setTok(e.target.value)} />
              <button className="btn" onClick={() => { setToken(token.trim()); location.reload(); }}>Save</button>
            </div>
          </Card>
        </div>
      )}
    </>
  );
}
