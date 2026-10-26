import {
  Activity, Ban, Bot, Brain, CalendarClock, CalendarPlus, CheckCircle2, ClipboardList, Cloud,
  FlaskConical, GitBranch, GitMerge, Hourglass, ListTodo, Play, Plus, Puzzle, ScanEye, Sparkles,
  Terminal, TriangleAlert, XCircle, Zap, type LucideIcon,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { Adapter, Agent, BuildLog, ProviderStatus, Run, Schedule, Task } from "../api/types";
import { Badge, Card, Empty, ErrorNote, fmtTime } from "../components/ui";
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

type Tone = "accent" | "info" | "ok" | "warn" | "bad" | "muted";

// Full class names so Tailwind can see them.
const CHIP: Record<Tone, string> = {
  accent: "bg-accent-soft text-accent",
  info: "bg-info/10 text-info",
  ok: "bg-ok/10 text-ok",
  warn: "bg-warn/10 text-warn",
  bad: "bg-bad/10 text-bad",
  muted: "bg-panel-2 text-muted",
};
const BAR: Record<Tone, string> = {
  accent: "bg-accent", info: "bg-info", ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", muted: "bg-line-strong",
};
const DOT: Record<"ok" | "warn" | "off", string> = { ok: "bg-ok", warn: "bg-warn", off: "bg-line-strong" };

const STATUS_TONE: [string, Tone][] = [
  ["in_progress", "warn"], ["queued", "info"], ["review", "accent"], ["blocked", "bad"],
  ["planned", "muted"], ["draft", "muted"], ["accepted", "ok"], ["rejected", "bad"],
];

function IconChip({ icon: Icon, tone, size = "md" }: { icon: LucideIcon; tone: Tone; size?: "sm" | "md" }) {
  const box = size === "sm" ? "h-7 w-7 rounded-lg" : "h-10 w-10 rounded-xl";
  return (
    <span className={`flex shrink-0 items-center justify-center ${box} ${CHIP[tone]}`}>
      <Icon size={size === "sm" ? 15 : 19} strokeWidth={1.9} aria-hidden="true" />
    </span>
  );
}

function StatTile({ label, value, icon, tone, to }: { label: string; value: number; icon: LucideIcon; tone: Tone; to: string }) {
  return (
    <Link to={to} className="card group flex items-center gap-3 px-4 py-3.5 transition hover:-translate-y-0.5 hover:border-accent/40">
      <IconChip icon={icon} tone={value ? tone : "muted"} />
      <div className="min-w-0">
        <div className="truncate text-xs text-ink-2">{label}</div>
        <div className="text-2xl font-semibold tabular-nums leading-tight">{value}</div>
      </div>
    </Link>
  );
}

function StatusChart({ counts }: { counts: Record<string, number> }) {
  const parts = STATUS_TONE.map(([s, tone]) => ({ s, tone, n: counts[s] ?? 0 })).filter((p) => p.n);
  const total = parts.reduce((a, p) => a + p.n, 0);
  if (!total) return <Empty>No tasks yet.</Empty>;
  return (
    <>
      <div className="flex h-3 overflow-hidden rounded-full bg-panel-2">
        {parts.map((p) => (
          <div key={p.s} className={`${BAR[p.tone]} border-r-2 border-panel last:border-r-0`} style={{ width: `${(p.n / total) * 100}%` }}
            title={`${p.s.replace(/_/g, " ")}: ${p.n}`} />
        ))}
      </div>
      <ul className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        {parts.map((p) => (
          <li key={p.s}>
            <Link to={`/tasks?status=${p.s}`} className="flex items-center gap-2 hover:text-accent">
              <span className={`h-2.5 w-2.5 rounded-full ${BAR[p.tone]}`} />
              <span className="text-ink-2">{p.s.replace(/_/g, " ")}</span>
              <span className="ml-auto font-medium tabular-nums">{p.n}</span>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}

function TaskList({ tasks, empty }: { tasks: Task[]; empty: string }) {
  if (!tasks.length) return <Empty>{empty}</Empty>;
  return (
    <ul className="divide-y divide-line">
      {tasks.map((t) => (
        <li key={t.id} className="flex items-center justify-between gap-2 py-2 text-sm">
          <Link to={`/tasks/${t.id}`} className="truncate hover:text-accent">
            <span className="mono text-muted">{t.short_id}</span> {t.title}
          </Link>
          <Badge value={t.status} />
        </li>
      ))}
    </ul>
  );
}

const PROVIDER_ICON: Record<string, LucideIcon> = {
  anthropic: Sparkles, claude_cli: Terminal, openai_compatible: Cloud, mock: FlaskConical,
};

function eventIcon(event: string): [LucideIcon, Tone] {
  const rules: [RegExp, LucideIcon, Tone][] = [
    [/fail|error|timed_out|timeout|reject/, XCircle, "bad"],
    [/block|needs_human|escalat/, TriangleAlert, "warn"],
    [/merge/, GitMerge, "ok"],
    [/accept|approv|pass/, CheckCircle2, "ok"],
    [/review/, ScanEye, "accent"],
    [/verif|test/, ClipboardList, "info"],
    [/memory/, Brain, "accent"],
    [/schedule/, CalendarClock, "info"],
    [/agent/, Bot, "accent"],
    [/worktree|branch/, GitBranch, "muted"],
    [/run/, Play, "info"],
    [/task/, ListTodo, "muted"],
  ];
  const hit = rules.find(([re]) => re.test(event));
  return hit ? [hit[1], hit[2]] : [Activity, "muted"];
}

function greeting(): string {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

export default function Dashboard() {
  const { data, error } = useApi<Dash>("/dashboard", 4000);
  const schedules = useApi<Schedule[]>("/schedules", 30000);
  const agents = useApi<Agent[]>("/agents", 30000);
  const upcoming = (schedules.data ?? [])
    .filter((s) => s.enabled && s.next_run_at)
    .sort((a, b) => a.next_run_at!.localeCompare(b.next_run_at!))
    .slice(0, 5);
  const agentName = (id: number | null) => agents.data?.find((a) => a.id === id)?.name;

  return (
    <>
      <section className="card relative mb-5 overflow-hidden px-6 py-6">
        <div className="pointer-events-none absolute -top-24 -right-16 h-64 w-64 rounded-full opacity-20 blur-3xl"
          style={{ background: "var(--gradient-brand)" }} aria-hidden="true" />
        <div className="relative flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-sm text-ink-2">{greeting()}</p>
            <h1 className="mt-1 text-3xl font-bold tracking-tight">
              Your <span className="text-gradient">orchestrator</span> at a glance
            </h1>
            <p className="mt-1.5 text-sm text-ink-2">Runs, queue, gates and health, refreshed every few seconds.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Link to="/tasks?new=1" className="btn btn-primary"><Plus size={15} aria-hidden="true" /> New task</Link>
            <Link to="/agents" className="btn"><Bot size={15} aria-hidden="true" /> Run an agent</Link>
            <Link to="/schedules" className="btn"><CalendarPlus size={15} aria-hidden="true" /> New schedule</Link>
          </div>
        </div>
      </section>

      <ErrorNote error={error} />
      {data && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:col-span-3">
            <StatTile label="Active runs" value={data.active_runs.length} icon={Zap} tone="warn" to="/tasks?status=in_progress" />
            <StatTile label="Queued" value={data.queued.length} icon={Hourglass} tone="info" to="/tasks?status=queued" />
            <StatTile label="In review" value={data.review.length} icon={ScanEye} tone="accent" to="/tasks?status=review" />
            <StatTile label="Blocked" value={data.blocked.length} icon={Ban} tone="bad" to="/tasks?status=blocked" />
            <StatTile label="Failed" value={data.failed.length} icon={XCircle} tone="bad" to="/tasks" />
            <StatTile label="Accepted" value={data.counts.accepted ?? 0} icon={CheckCircle2} tone="ok" to="/tasks?status=accepted" />
            <StatTile label="Planned" value={data.counts.planned ?? 0} icon={ListTodo} tone="accent" to="/tasks?status=planned" />
            <StatTile label="Memory to review" value={data.memory_pending} icon={Brain} tone="accent" to="/memory" />
          </div>

          <Card title="Tasks by status" className="xl:col-span-2"><StatusChart counts={data.counts} /></Card>
          <Card title="Next scheduled runs" actions={<Link to="/schedules" className="text-xs text-accent">all schedules →</Link>}>
            {upcoming.length ? (
              <ul className="space-y-2.5 text-sm">
                {upcoming.map((s) => (
                  <li key={s.id} className="flex items-center gap-3">
                    <IconChip icon={s.agent_id ? Bot : CalendarClock} tone={s.agent_id ? "accent" : "info"} size="sm" />
                    <div className="min-w-0">
                      <div className="truncate font-medium">{s.name}</div>
                      <div className="truncate text-xs text-muted">{agentName(s.agent_id) ? `agent ${agentName(s.agent_id)} · ` : ""}{fmtTime(s.next_run_at)}</div>
                    </div>
                  </li>
                ))}
              </ul>
            ) : <Empty>Nothing scheduled.</Empty>}
          </Card>

          <Card title="Active runs">
            {data.active_runs.length ? (
              <ul className="divide-y divide-line text-sm">
                {data.active_runs.map((r) => (
                  <li key={r.id} className="flex items-center justify-between gap-2 py-2">
                    <Link className="flex items-center gap-2 hover:text-accent" to={`/tasks/${r.task_id}`}>
                      <span className="relative flex h-2 w-2"><span className="absolute h-full w-full animate-ping rounded-full bg-warn opacity-60" /><span className="h-2 w-2 rounded-full bg-warn" /></span>
                      {r.short_id} · {r.harness_adapter ?? "direct"}
                    </Link>
                    <Badge value={r.status} />
                  </li>
                ))}
              </ul>
            ) : <Empty>No active runs.</Empty>}
          </Card>
          <Card title="Queued"><TaskList tasks={data.queued} empty="Queue is empty." /></Card>
          <Card title="Blocked (waiting for a decision)"><TaskList tasks={data.blocked} empty="Nothing blocked." /></Card>

          <Card title="Providers" actions={<Link to="/models" className="text-xs text-accent">manage →</Link>}>
            <ul className="space-y-2.5 text-sm">
              {data.providers.map((p) => {
                const state = !p.enabled ? "off" : p.configured ? "ok" : "warn";
                return (
                  <li key={p.id} className="flex items-center gap-3">
                    <IconChip icon={PROVIDER_ICON[p.kind] ?? Cloud} tone={state === "ok" ? "accent" : "muted"} size="sm" />
                    <span className="min-w-0 flex-1 truncate">{p.name} <span className="text-xs text-muted">{p.kind}</span></span>
                    <span className="flex items-center gap-1.5 text-xs text-ink-2">
                      <span className={`h-2 w-2 rounded-full ${DOT[state]}`} />
                      {state === "off" ? "disabled" : state === "ok" ? "ready" : "not configured"}
                    </span>
                  </li>
                );
              })}
            </ul>
          </Card>
          <Card title="Adapters" actions={<Link to="/adapters" className="text-xs text-accent">manage →</Link>}>
            <ul className="space-y-2.5 text-sm">
              {data.adapters.map((a) => {
                const state = !a.enabled ? "off" : a.detected ? "ok" : "warn";
                return (
                  <li key={a.id} className="flex items-center gap-3">
                    <IconChip icon={a.name === "mock" ? FlaskConical : Puzzle} tone={state === "ok" ? "info" : "muted"} size="sm" />
                    <span className="min-w-0 flex-1 truncate">{a.name}{a.experimental && <span className="ml-1.5 text-xs text-warn">exp</span>}</span>
                    <span className="flex items-center gap-1.5 text-xs text-ink-2">
                      <span className={`h-2 w-2 rounded-full ${DOT[state === "warn" ? "off" : state]}`} />
                      {state === "off" ? "disabled" : state === "ok" ? "detected" : "not found"}
                    </span>
                  </li>
                );
              })}
            </ul>
          </Card>
          <Card title="Failed runs"><TaskList tasks={data.failed} empty="No failed tasks." /></Card>

          <Card title="Recent activity" className="xl:col-span-3"
            actions={<Link to="/build-log" className="text-xs text-accent">full build log →</Link>}>
            {data.recent_activity.length ? (
              <ul className="divide-y divide-line text-sm">
                {data.recent_activity.map((e) => {
                  const [Icon, tone] = eventIcon(e.event);
                  return (
                    <li key={e.id} className="grid grid-cols-[auto_10rem_1fr] items-center gap-3 py-2 md:grid-cols-[auto_11rem_6rem_10rem_1fr]">
                      <IconChip icon={Icon} tone={tone} size="sm" />
                      <span className="text-xs text-muted">{fmtTime(e.timestamp)}</span>
                      <span className="hidden text-xs text-ink-2 md:inline">{e.role}</span>
                      <span className="mono hidden text-xs text-accent md:inline">{e.event}</span>
                      <span className="truncate text-ink-2">{e.details}</span>
                    </li>
                  );
                })}
              </ul>
            ) : <Empty>No activity yet.</Empty>}
          </Card>
        </div>
      )}
    </>
  );
}
