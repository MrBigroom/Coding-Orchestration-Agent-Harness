import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, post } from "../api/client";
import type { Adapter, ModelProfile, RecoveryCommit, TaskDetail as TD } from "../api/types";
import { Code, Diff } from "../components/Code";
import { Badge, Card, ConfirmButton, Empty, ErrorNote, PageHeader, fmtTime } from "../components/ui";
import { useAction, useApi } from "../hooks/useApi";
import { useRunLogs } from "../hooks/useRunLogs";

const TABS = ["overview", "logs", "diff", "handoff", "review", "verification", "memory"] as const;
type Tab = (typeof TABS)[number];

function Artifact({ runId, name, json }: { runId: number; name: string; json?: boolean }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setText(null);
    setErr(null);
    api<unknown>(`/artifacts/${runId}/${name}`)
      .then((d) => setText(typeof d === "string" ? d : JSON.stringify(d, null, 2)))
      .catch((e) => setErr((e as Error).message));
  }, [runId, name]);
  if (err) return <div className="text-sm text-muted">{name}: {err}</div>;
  if (text === null) return <div className="text-sm text-muted">loading {name}…</div>;
  if (!text.trim()) return <Empty>{name} is empty.</Empty>;
  return <Code code={text} lang={json ? "json" : null} path={name} />;
}

function RecoveryNote({ commits }: { commits: RecoveryCommit[] }) {
  if (!commits.length) return null;
  const paths = Array.from(new Set(commits.flatMap((c) => c.files.map((f) => f.path))));
  return (
    <div className="mb-2 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm">
      <div className="mb-1 font-medium text-warn">
        {commits.length} recovery commit{commits.length === 1 ? "" : "s"} — committed by the orchestrator after an aborted turn, never self-checked by the Worker
      </div>
      <ul className="list-disc pl-5 text-xs">
        {commits.map((c) => (
          <li key={c.sha}>
            <span className="mono">{c.short}</span> {c.subject}
            {c.recovery_run ? <span className="text-muted"> (run {c.recovery_run})</span> : null}
          </li>
        ))}
      </ul>
      {paths.length > 0 && (
        <div className="mt-1 text-xs text-muted">paths below include harness-captured wreckage: <span className="mono">{paths.join(", ")}</span></div>
      )}
    </div>
  );
}

function DiffView({ runId, recovery = [] }: { runId: number; recovery?: RecoveryCommit[] }) {
  const [text, setText] = useState("");
  useEffect(() => { api<string>(`/artifacts/${runId}/patch.diff`).then((d) => setText(String(d ?? ""))).catch(() => setText("")); }, [runId]);
  if (!text.trim()) return <><RecoveryNote commits={recovery} /><Empty>No diff for this run.</Empty></>;
  return <><RecoveryNote commits={recovery} /><Diff diff={text} /></>;
}

function Logs({ runId }: { runId: number }) {
  const { lines, status, mode } = useRunLogs(runId);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => end.current?.scrollIntoView({ block: "nearest" }), [lines.length]);
  return (
    <div>
      <div className="mb-2 flex gap-3 text-xs text-muted"><span>transport: {mode}</span><span>status: <Badge value={status} /></span></div>
      <div className="mono max-h-[32rem] overflow-auto rounded-md border border-line bg-bg p-3 text-xs leading-relaxed">
        {lines.length ? lines.map((l, i) => <div key={i} className={l.stream === "stderr" ? "text-warn" : ""}>{l.line}</div>) : <span className="text-muted">no output yet</span>}
        <div ref={end} />
      </div>
    </div>
  );
}

export default function TaskDetail() {
  const { id } = useParams();
  const { data: task, error, reload } = useApi<TD>(`/tasks/${id}`, 3000);
  const { data: adapters } = useApi<Adapter[]>("/adapters");
  const { data: profiles } = useApi<ModelProfile[]>("/model-profiles");
  const [tab, setTab] = useState<Tab>("overview");
  const [runId, setRunId] = useState<number | null>(null);
  const [retryWith, setRetryWith] = useState({ harness_adapter: "", provider_profile: "" });
  const act = useAction();
  const run = useMemo(() => task?.runs.find((r) => r.id === runId) ?? task?.runs[0] ?? null, [task, runId]);

  if (!task) return <><ErrorNote error={error} /><div className="text-muted">Loading…</div></>;
  const action = (verb: string, body?: unknown) => act.run(async () => { await post(`/tasks/${task.id}/${verb}`, body); await reload(); });
  const pending = task.memory_suggestions.filter((m) => !m.approved && !m.rejected);

  return (
    <>
      <PageHeader title={`${task.short_id} · ${task.title}`}
        subtitle={`${task.kind} · ${task.branch_name ?? "no branch"} · source ${task.source}`}
        actions={<>
          <Badge value={task.status} />
          {(task.recovery_commits?.length ?? 0) > 0 && <Badge value="recovery" />}
          {["planned", "queued", "rejected", "blocked"].includes(task.status) && <button className="btn btn-primary" onClick={() => action("run")}>Run</button>}
          {task.status === "review" && <button className="btn btn-primary" onClick={() => action("accept", { reason: "accepted in GUI" })}>Accept</button>}
          {task.status === "blocked" && <ConfirmButton label="Accept" danger={false} message="Accept despite the blocker? This records a human decision in the build log." onConfirm={() => action("accept", { reason: "accepted in GUI after block" })} />}
          {["review", "blocked"].includes(task.status) && <ConfirmButton label="Reject" message="Reject this task? It can be retried later." onConfirm={() => action("reject", { reason: "rejected in GUI" })} />}
          <button className="btn" onClick={() => action("escalate")}>Escalate</button>
          {["queued", "in_progress"].includes(task.status) && <ConfirmButton label="Cancel" message="Terminate the running worker?" onConfirm={() => action("cancel")} />}
        </>} />
      <ErrorNote error={act.error} />
      {task.merge_command && (
        <div className="mb-4 rounded-md border border-ok/40 bg-ok/10 p-3 text-sm">
          <div className="mb-1 font-medium text-ok">Accepted — merge gate disabled. Merge manually:</div>
          <code className="mono text-xs">{task.merge_command}</code>
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`rounded-md px-3 py-1 text-sm ${tab === t ? "bg-accent/15 text-accent" : "text-muted hover:text-ink"}`}>
            {t}{t === "memory" && pending.length ? ` (${pending.length})` : ""}
          </button>
        ))}
        {task.runs.length > 0 && (
          <select className="input ml-auto w-auto" value={run?.id ?? ""} onChange={(e) => setRunId(Number(e.target.value))}>
            {task.runs.map((r) => <option key={r.id} value={r.id}>{r.short_id} · {r.status}</option>)}
          </select>
        )}
      </div>

      {tab === "overview" && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Card title="Phase & Definition of Done">
            {task.description && <p className="mb-3 whitespace-pre-wrap text-sm">{task.description}</p>}
            <div className="label">Definition of Done</div>
            {task.definition_of_done.length ? <ol className="mb-3 list-decimal pl-5 text-sm">{task.definition_of_done.map((d, i) => <li key={i}>{d}</li>)}</ol> : <p className="mb-3 text-sm text-muted">none</p>}
            {task.acceptance_criteria.length > 0 && <><div className="label">Acceptance criteria</div><ul className="mb-3 list-disc pl-5 text-sm">{task.acceptance_criteria.map((d, i) => <li key={i}>{d}</li>)}</ul></>}
            {task.frozen_contracts.length > 0 && <><div className="label">Frozen contracts</div><ul className="mb-3 list-disc pl-5 text-sm">{task.frozen_contracts.map((d, i) => <li key={i}>{d}</li>)}</ul></>}
            {task.out_of_scope.length > 0 && <><div className="label">Out of scope</div><ul className="list-disc pl-5 text-sm">{task.out_of_scope.map((d, i) => <li key={i}>{d}</li>)}</ul></>}
          </Card>
          <Card title="Gates">
            <dl className="grid grid-cols-2 gap-y-2 text-sm">
              <dt className="text-muted">Handoff</dt><dd><Badge value={task.handoff_status} /></dd>
              <dt className="text-muted">Review</dt><dd><Badge value={task.review_status} /></dd>
              <dt className="text-muted">Verification</dt><dd><Badge value={task.verification_status} /></dd>
              <dt className="text-muted">Merge</dt><dd><Badge value={task.merge_status} /></dd>
              <dt className="text-muted">Risk / priority</dt><dd><Badge value={task.risk} /> {task.priority}</dd>
              <dt className="text-muted">Tags</dt><dd className="text-xs">{task.tags.join(", ") || "—"}</dd>
              <dt className="text-muted">Assigned</dt><dd className="text-xs">{task.assigned_harness_adapter ?? "auto"} / {task.assigned_provider_profile ?? "auto"}</dd>
            </dl>
            <div className="mt-4 border-t border-line pt-3">
              <div className="label">Retry with a different harness / model</div>
              <div className="flex flex-wrap gap-2">
                <select className="input w-auto" value={retryWith.harness_adapter} onChange={(e) => setRetryWith({ ...retryWith, harness_adapter: e.target.value })}>
                  <option value="">harness: auto</option>{adapters?.map((a) => <option key={a.name} value={a.name}>{a.name}</option>)}
                </select>
                <select className="input w-auto" value={retryWith.provider_profile} onChange={(e) => setRetryWith({ ...retryWith, provider_profile: e.target.value })}>
                  <option value="">profile: auto</option>{profiles?.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
                </select>
                <button className="btn" disabled={!["planned", "rejected", "blocked"].includes(task.status)}
                  onClick={() => action("retry", { harness_adapter: retryWith.harness_adapter || null, provider_profile: retryWith.provider_profile || null })}>Retry</button>
              </div>
            </div>
          </Card>
          <Card title="Runs" className="xl:col-span-2">
            {task.runs.length ? (
              <table className="w-full"><thead><tr>{["Run", "Status", "Harness", "Profile", "Exit", "Duration", "Started", "Command"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
                <tbody>{task.runs.map((r) => (
                  <tr key={r.id} className="cursor-pointer hover:bg-panel-2/50" onClick={() => { setRunId(r.id); setTab("logs"); }}>
                    <td className="td mono">{r.short_id}</td><td className="td"><Badge value={r.status} /></td>
                    <td className="td">{r.harness_adapter ?? "direct"}</td><td className="td">{r.provider_profile}</td>
                    <td className="td">{r.exit_code ?? "—"}</td><td className="td">{r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}</td>
                    <td className="td text-xs">{fmtTime(r.started_at)}</td>
                    <td className="td mono max-w-md truncate text-xs text-muted" title={r.command_preview ?? ""}>{r.command_preview}</td>
                  </tr>))}</tbody></table>
            ) : <Empty>No runs yet.</Empty>}
          </Card>
        </div>
      )}
      {tab !== "overview" && tab !== "memory" && !run && <Empty>No run selected.</Empty>}
      {run && tab === "logs" && <Card title={`Logs — ${run.short_id}`}><Logs runId={run.id} />{run.error && <p className="mt-2 text-sm text-bad">{run.error}</p>}</Card>}
      {run && tab === "diff" && <Card title={`Diff — ${run.short_id}`}><DiffView runId={run.id} recovery={task.recovery_commits ?? []} /></Card>}
      {run && tab === "handoff" && <div className="grid grid-cols-1 gap-4 xl:grid-cols-2"><Card title="handoff.md"><Artifact runId={run.id} name="handoff.md" /></Card><Card title="handoff.json"><Artifact runId={run.id} name="handoff.json" json /></Card></div>}
      {run && tab === "review" && <div className="grid grid-cols-1 gap-4 xl:grid-cols-2"><Card title="review.md"><Artifact runId={run.id} name="review.md" /></Card><Card title="review.json"><Artifact runId={run.id} name="review.json" json /></Card></div>}
      {run && tab === "verification" && <div className="grid grid-cols-1 gap-4 xl:grid-cols-2"><Card title="verification.md"><Artifact runId={run.id} name="verification.md" /></Card><Card title="test.log"><Artifact runId={run.id} name="test.log" /></Card></div>}
      {tab === "memory" && (
        <Card title="Memory suggestions from this task's handoffs">
          {task.memory_suggestions.length ? (
            <ul className="divide-y divide-line">{task.memory_suggestions.map((m) => (
              <li key={m.id} className="flex items-start justify-between gap-3 py-2 text-sm">
                <div><div>{m.content}</div><div className="text-xs text-muted">{m.category}/{m.scope}{m.project_slug ? ` · ${m.project_slug}` : ""}</div></div>
                <div className="flex shrink-0 gap-1">
                  {m.approved ? <Badge value="approved" /> : m.rejected ? <Badge value="rejected" /> : <>
                    <button className="btn btn-primary" onClick={() => act.run(async () => { await post(`/memory/${m.id}/approve`); await reload(); })}>Approve</button>
                    <button className="btn btn-danger" onClick={() => act.run(async () => { await post(`/memory/${m.id}/reject`); await reload(); })}>Reject</button>
                  </>}
                </div>
              </li>))}</ul>
          ) : <Empty>No memory suggestions.</Empty>}
          <p className="mt-3 text-xs text-muted">Manage all entries on the <Link className="text-accent" to="/memory">Memory</Link> page.</p>
        </Card>
      )}
    </>
  );
}
