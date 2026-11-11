import { getToken, patch, post, setToken } from "../api/client";
import { Badge, Card, ErrorNote, PageHeader, ThemeSwitch, Toggle, fmtTime } from "../components/ui";
import { Code } from "../components/Code";
import { FONTS, useFont, type FontSlot } from "../fonts";
import { DARK_THEMES, LIGHT_THEMES, setCodeTheme, useCodeThemes, type ThemeMode } from "../highlight";
import { useAction, useApi } from "../hooks/useApi";
import { useState } from "react";

type S = Record<string, string | number | boolean | null>;
type SyncResult = {
  pulled: Record<string, number>;
  pushed: Record<string, number>;
};
type SyncStatus = {
  configured: boolean;
  enabled: boolean;
  running: boolean;
  interval_seconds: number;
  tracked: number;
  pending: number;
  conflicts: number;
  synced: number;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_result: SyncResult | null;
  consecutive_failures: number;
  last_failure_at: string | null;
  last_failure_kind: string | null;
  last_failure_error: string | null;
  stopped: boolean;
  stopped_reason: string | null;
  next_retry_at: string | null;
  backoff_seconds: number;
  notify_failures: number;
  last_notify_error: string | null;
  last_notify_failed_at: string | null;
};

const BOOLS: [string, string][] = [
  ["merge_enabled", "Merge gate enabled (allows --no-ff merges of accepted phase branches)"],
  ["auto_merge_enabled", "Auto-merge after APPROVED + live verification (requires merge gate)"],
  ["supervisor_microfixes_enabled", "Allow Supervisor micro-fixes (otherwise code changes go back to the Worker)"],
  ["scheduler_enabled", "Scheduler enabled (takes effect on restart)"],
  ["worker_enabled", "Background queue worker enabled"],
  ["allow_gui_commands", "Allow running project test/lint/build commands from the GUI"],
  ["allow_interactive_sessions", "Allow interactive harness sessions in Chat (agents get shell access)"],
  ["allow_unattended_sessions", "Allow standalone agent runs with nobody watching (skips the task pipeline and review)"],
  ["memory_budget_enabled", "Cap how much approved memory is injected per prompt section (memory is static, so uncapped growth squeezes diff/verification evidence)"],
];
// T-9. Keys are the same ones settings_service exposes; the master switch is OFF on a new
// install and every trigger can be individually disabled.
const NOTIFICATION_TRIGGERS: [string, string, boolean][] = [
  ["notification_run_completed", "Run completed (success path)", false],
  ["notification_run_failed", "Run failed", false],
  ["notification_run_timeout", "Run timed out", false],
  ["notification_run_cancelled", "Run cancelled", false],
  ["notification_standalone_run", "Standalone report ready", false],
  ["notification_task_review", "Task reached review / awaiting acceptance", false],
  ["notification_task_blocked", "Task blocked", false],
  ["notification_escalated", "Task escalated", false],
  ["notification_merge_candidate", "Merge candidate ready", false],
  ["notification_merged", "Branch merged", false],
  ["notification_merge_refused", "Merge refused", false],
  ["notification_sync_failed", "Cloud sync needs attention", false],
  ["notification_worktree_created", "Noisy: worktree created", true],
  ["notification_recovery_commit", "Noisy: recovery commit", true],
  ["notification_supervisor_verification", "Noisy: supervisor verification", true],
  ["notification_architect_review", "Noisy: architect review", true],
  ["notification_nits_deferred", "Noisy: review nits deferred", true],
  ["notification_schedule_fired", "Noisy: schedule fired", true],
];

const NUMS: [string, string][] = [
  ["max_parallel_runs", "Max parallel runs"],
  ["max_parallel_runs_per_project", "Max parallel runs per project"],
  ["max_review_rounds", "Max verified review rounds before NEEDS_HUMAN"],
  ["max_run_attempts_per_task", "Max run attempts per task (timeouts and failures included)"],
  ["max_memory_tokens_global", "Cap on the injected global memory section (tokens, 0 = no cap)"],
  ["max_memory_tokens_role", "Cap on the injected role memory section (tokens, 0 = no cap)"],
  ["max_memory_tokens_project", "Cap on the injected project memory section (tokens, 0 = no cap)"],
];

export default function Settings() {
  const { data, error, setData } = useApi<S>("/settings");
  const { data: sync, error: syncError, reload: reloadSync } = useApi<SyncStatus>("/sync/status", 5000);
  const act = useAction();
  const syncAct = useAction();
  const [token, setTok] = useState(getToken());
  const update = (key: string, value: unknown) => act.run(async () => setData(await patch<S>("/settings", { values: { [key]: value } })));
  const updateSync = (key: string, value: unknown) => act.run(async () => {
    setData(await patch<S>("/settings", { values: { [key]: value } }));
    await reloadSync();
  });
  const runSync = () => syncAct.run(async () => {
    await post<SyncResult>("/sync/run");
    await reloadSync();
  });
  return (
    <>
      <PageHeader title="Settings" subtitle="Defaults come from ORCHESTRATOR_* env vars; changes here are stored in SQLite." />
      <ErrorNote error={error ?? act.error ?? syncError ?? syncAct.error} />
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
          <Card title="Notifications">
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-4">
                <span className="text-sm">Desktop notifications<span className="block text-xs text-muted">Off by default. Freedesktop D-Bus; every decision is also kept in the Notifications list.</span></span>
                <Toggle checked={Boolean(data.notifications_enabled)} onChange={(v) => update("notifications_enabled", v)} />
              </div>
              <div className="space-y-2 border-t border-line pt-3">
                {NOTIFICATION_TRIGGERS.map(([k, label, noisy]) => (
                  <div key={k} className="flex items-center justify-between gap-4">
                    <span className={`text-sm ${noisy ? "text-muted" : ""}`}>{label}</span>
                    <Toggle checked={Boolean(data[k])} onChange={(v) => update(k, v)} />
                  </div>
                ))}
              </div>
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
          <Card title="Cloud Sync" className="xl:col-span-2" actions={sync && (
            <Badge value={sync.stopped ? "stopped" : sync.running ? "running" : sync.configured ? "configured" : "not configured"}
              tone={sync.stopped ? "bad" : sync.running ? "info" : sync.configured ? "ok" : "muted"} />
          )}>
            {!sync ? <p className="text-sm text-muted">Loading synchronization status…</p> : (
              <div className="space-y-5">
                <p className="text-sm text-muted">
                  SQLite remains authoritative. Supabase synchronization is optional and never uploads data until credentials are configured.
                </p>
                <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                  <div className="space-y-4">
                    <div className="flex items-center justify-between gap-4">
                      <span className="text-sm">Automatic background sync
                        <span className="block text-xs text-muted">Off by default; applies without restarting the server.</span>
                      </span>
                      <Toggle checked={sync.enabled} disabled={!sync.configured || act.busy}
                        onChange={(v) => updateSync("supabase_sync_enabled", v)} />
                    </div>
                    <div className="flex items-center justify-between gap-4">
                      <label className="text-sm" htmlFor="sync-interval">Sync interval
                        <span className="block text-xs text-muted">15 seconds to 24 hours</span>
                      </label>
                      <div className="flex items-center gap-2">
                        <input id="sync-interval" className="input w-28" type="number" min={15} max={86400}
                          defaultValue={sync.interval_seconds} disabled={act.busy}
                          onBlur={(e) => Number(e.target.value) !== sync.interval_seconds &&
                            updateSync("supabase_sync_interval_seconds", Number(e.target.value))} />
                        <span className="text-xs text-muted">seconds</span>
                      </div>
                    </div>
                    <button className="btn btn-primary" disabled={!sync.configured || sync.running || syncAct.busy}
                      onClick={runSync}>{syncAct.busy || sync.running ? "Syncing…" : "Sync now"}</button>
                    <p className="text-xs text-muted">Manual sync works even when automatic sync is off.</p>
                    {!sync.configured && (
                      <p className="rounded-md border border-line bg-panel-2 p-3 text-xs text-muted">
                        Add the device-local Supabase URL, publishable key, email, and password to <span className="mono">.env</span>,
                        then restart the server. Follow <span className="mono">docs/cloud-sync.md</span>. Credentials are never stored in SQLite or synchronized.
                      </p>
                    )}
                  </div>
                  <div>
                    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                      <dt className="text-muted">Tracked records</dt><dd>{sync.tracked}</dd>
                      <dt className="text-muted">Pending upload</dt><dd>{sync.pending}</dd>
                      <dt className="text-muted">Synchronized</dt><dd>{sync.synced}</dd>
                      <dt className="text-muted">Conflicts</dt><dd className={sync.conflicts ? "text-bad" : ""}>{sync.conflicts}</dd>
                      <dt className="text-muted">Last success</dt><dd>{fmtTime(sync.last_success_at)}</dd>
                      <dt className="text-muted">Consecutive failures</dt>
                      <dd className={sync.consecutive_failures ? "text-bad" : ""}>{sync.consecutive_failures}</dd>
                      <dt className="text-muted">Next retry</dt><dd>{fmtTime(sync.next_retry_at)}</dd>
                    </dl>
                    {sync.last_result && (
                      <p className="mt-3 text-xs text-muted">
                        Last transfer: {sync.last_result.pulled.applied ?? 0} remote records applied, {sync.last_result.pushed.pushed ?? 0} local records uploaded.
                      </p>
                    )}
                    {sync.stopped && (
                      <p className="mt-3 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">
                        Automatic sync is stopped: {sync.stopped_reason ?? sync.last_failure_error ?? "unknown reason"}
                        <span className="mt-1 block text-muted">
                          {sync.last_failure_kind === "permanent" ? (
                            <>Fix the cause, then run <span className="mono">orchestrator sync reinit --dry-run</span>,
                              then <span className="mono"> --apply</span>, or retry with Sync now.</>
                          ) : (
                            <>Retry with Sync now; a transient failure is not repaired by reinit.</>
                          )}
                        </span>
                      </p>
                    )}
                    {!sync.stopped && sync.consecutive_failures > 0 && (
                      <p className="mt-3 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">
                        {sync.consecutive_failures} consecutive failure(s): {sync.last_failure_error ?? "unknown error"}
                        <span className="mt-1 block text-muted">
                          Retrying with exponential backoff; next attempt {fmtTime(sync.next_retry_at)}.
                        </span>
                      </p>
                    )}
                    {sync.last_notify_error && (
                      <p className="mt-3 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">
                        Sync alerts are not reaching the operator: {sync.last_notify_error}
                        <span className="mt-1 block text-muted">
                          {sync.notify_failures} failed attempt(s); retrying with capped backoff
                          {sync.last_notify_failed_at ? ` (last attempt ${fmtTime(sync.last_notify_failed_at)})` : ""}.
                        </span>
                      </p>
                    )}
                    {sync.last_error && <p className="mt-3 rounded-md border border-bad/40 bg-bad/10 p-3 text-xs text-bad">{sync.last_error}</p>}
                  </div>
                </div>
                <p className="border-t border-line pt-3 text-xs text-muted">
                  Database records only. Credentials, local paths, repositories, worktrees, artifacts, and evidence files stay on this device.
                </p>
              </div>
            )}
          </Card>
          <Card title="Appearance">
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm">Theme <span className="block text-xs text-muted">Saved in this browser. System follows your OS setting.</span></span>
              <ThemeSwitch full />
            </div>
            <FontPicker slot="text" label="Text font" hint="The interface and chat replies." sample="The quick brown fox jumps over the lazy dog." />
            <FontPicker slot="code" label="Code font" hint="Code, paths, commands and model names." sample="const ok = a !== b && x >= 0 -> 0O 1lI {}" />
            <CodeThemePicker />
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

function FontPicker({ slot, label, hint, sample }: { slot: FontSlot; label: string; hint: string; sample: string }) {
  const [font, setFont] = useFont(slot);
  return (
    <div className="mt-4 border-t border-line pt-4">
      <div className="flex items-center justify-between gap-4">
        <label htmlFor={`font-${slot}`} className="text-sm">{label} <span className="block text-xs text-muted">{hint} Saved in this browser.</span></label>
        <select id={`font-${slot}`} className="input w-auto" value={font.id} onChange={(e) => setFont(e.target.value)}>
          {FONTS[slot].map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
        </select>
      </div>
      <p className="mt-2 truncate rounded-lg bg-panel-2 px-3 py-2 text-sm" style={{ fontFamily: font.stack }}>{sample}</p>
    </div>
  );
}

const THEME_SAMPLE = `def deflect(frames: int, window: int = 12) -> bool:
    """True inside the deflect window."""
    return 0 < frames <= window  # perfect or normal`;

function CodeThemePicker() {
  const themes = useCodeThemes();
  const select = (mode: ThemeMode, list: typeof LIGHT_THEMES) => (
    <label className="flex items-center gap-2 text-xs text-muted">
      {mode === "light" ? "Light" : "Dark"}
      <select className="input w-auto" value={themes[mode]} onChange={(e) => setCodeTheme(mode, e.target.value)}>
        {list.map((t) => <option key={t.id} value={t.id}>{t.displayName}</option>)}
      </select>
    </label>
  );
  return (
    <div className="mt-4 border-t border-line pt-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-sm">Code colors <span className="block text-xs text-muted">VS Code themes for code and diffs, one for each app theme. Saved in this browser.</span></span>
        <div className="flex flex-wrap gap-3">{select("light", LIGHT_THEMES)}{select("dark", DARK_THEMES)}</div>
      </div>
      <Code code={THEME_SAMPLE} lang="python" className="mt-2" />
    </div>
  );
}
