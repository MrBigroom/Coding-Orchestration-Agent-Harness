import { Link } from "react-router-dom";
import type { Adapter, BuildLog, ProviderStatus, Run, Task } from "../api/types";
import { Badge, Card, Empty, ErrorNote, PageHeader, fmtTime } from "../components/ui";
import { useApi } from "../hooks/useApi";

interface Dash {
  active_runs: Run[];
  queued: Task[];
  blocked: Task[];
  review: Task[];
  failed: Task[];
  counts: Record<string, number>;
  providers: ProviderStatus[];
  adapters: Adapter[];
  memory_pending: number;
  recent_activity: BuildLog[];
}

function TaskList({ tasks, empty }: { tasks: Task[]; empty: string }) {
  if (!tasks.length) return <Empty>{empty}</Empty>;
  return (
    <ul className="divide-y divide-line">
      {tasks.map((t) => (
        <li key={t.id} className="flex items-center justify-between gap-2 py-1.5 text-sm">
          <Link to={`/tasks/${t.id}`} className="truncate hover:text-accent">
            <span className="mono text-muted">{t.short_id}</span> {t.title}
          </Link>
          <Badge value={t.status} />
        </li>
      ))}
    </ul>
  );
}

export default function Dashboard() {
  const { data, error } = useApi<Dash>("/dashboard", 4000);
  return (
    <>
      <PageHeader title="Dashboard" subtitle="Runs, queue, gates, and health at a glance." />
      <ErrorNote error={error} />
      {data && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:col-span-3">
            {[["Active runs", data.active_runs.length], ["Queued", data.queued.length],
              ["Blocked", data.blocked.length], ["Failed", data.failed.length],
              ["In review", data.review.length], ["Accepted", data.counts.accepted ?? 0],
              ["Memory pending", data.memory_pending], ["Planned", data.counts.planned ?? 0]].map(([k, v]) => (
              <div key={k} className="card px-4 py-3">
                <div className="text-xs uppercase tracking-wide text-muted">{k}</div>
                <div className="mt-1 text-2xl font-semibold">{v}</div>
              </div>
            ))}
          </div>
          <Card title="Active runs">
            {data.active_runs.length ? (
              <ul className="divide-y divide-line text-sm">
                {data.active_runs.map((r) => (
                  <li key={r.id} className="flex justify-between py-1.5">
                    <Link className="hover:text-accent" to={`/tasks/${r.task_id}`}>{r.short_id} · {r.harness_adapter ?? "direct"}</Link>
                    <Badge value={r.status} />
                  </li>
                ))}
              </ul>
            ) : <Empty>No active runs.</Empty>}
          </Card>
          <Card title="Queued"><TaskList tasks={data.queued} empty="Queue is empty." /></Card>
          <Card title="Blocked (waiting for a decision)"><TaskList tasks={data.blocked} empty="Nothing blocked." /></Card>
          <Card title="Failed runs"><TaskList tasks={data.failed} empty="No failed tasks." /></Card>
          <Card title="Providers">
            <ul className="space-y-1 text-sm">
              {data.providers.map((p) => (
                <li key={p.id} className="flex justify-between gap-2">
                  <span>{p.name} <span className="text-xs text-muted">{p.kind}</span></span>
                  {!p.enabled ? <Badge value="disabled" tone="muted" /> : p.configured ? <Badge value="ready" tone="ok" /> : <Badge value="not configured" tone="warn" />}
                </li>
              ))}
            </ul>
          </Card>
          <Card title="Adapters">
            <ul className="space-y-1 text-sm">
              {data.adapters.map((a) => (
                <li key={a.id} className="flex justify-between gap-2">
                  <span>{a.name}{a.experimental && <span className="ml-1 text-xs text-warn">exp</span>}</span>
                  {!a.enabled ? <Badge value="disabled" tone="muted" /> : a.detected ? <Badge value="detected" tone="ok" /> : <Badge value="not found" tone="muted" />}
                </li>
              ))}
            </ul>
          </Card>
          <Card title="Recent activity" className="xl:col-span-3"
            actions={<Link to="/build-log" className="text-xs text-accent">full build log →</Link>}>
            <ul className="divide-y divide-line text-sm">
              {data.recent_activity.map((e) => (
                <li key={e.id} className="grid grid-cols-[11rem_6rem_10rem_1fr] gap-3 py-1.5">
                  <span className="text-xs text-muted">{fmtTime(e.timestamp)}</span>
                  <span className="text-xs">{e.role}</span>
                  <span className="mono text-xs text-accent">{e.event}</span>
                  <span className="truncate text-muted">{e.details}</span>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      )}
    </>
  );
}
