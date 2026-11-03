import { useMemo, useState, type ReactNode } from "react";
import type { UsageDaily } from "../api/types";
import { Card, Empty, ErrorNote, PageHeader } from "../components/ui";
import { useApi } from "../hooks/useApi";

type Row = UsageDaily["rows"][number];
type Metric = "total" | "input" | "output" | "cached" | "requests";

const METRICS: [Metric, string][] = [
  ["total", "Total tokens"], ["input", "Input"], ["output", "Output"], ["cached", "Cached input"], ["requests", "Requests"],
];
const SOURCES: [string, string][] = [
  ["", "All sources"], ["task", "Tasks"], ["chat", "Chat"], ["standalone", "Standalone runs"], ["api", "Direct API calls"],
];
const RANGES = [7, 30, 90];
// Categorical slots in fixed order (see --series-N in index.css); a ninth model folds into "Other".
const SLOTS = 8;
const OTHER = "Other";

function value(r: Row, m: Metric): number {
  switch (m) {
    case "total": return r.input_tokens + r.output_tokens;
    case "input": return r.input_tokens;
    case "output": return r.output_tokens;
    case "cached": return r.cached_input_tokens;
    case "requests": return r.requests;
  }
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1000) return `${+(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

// A label starts at its day and runs right, so none starts in the last few columns.
function showLabel(i: number, count: number, every: number): boolean {
  return i % every === 0 && (count <= 7 || i <= count - Math.max(3, every / 2));
}

function dayLabel(iso: string, long = false): string {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, long ? { weekday: "short", month: "short", day: "numeric" } : { month: "short", day: "numeric" });
}

function Segmented<T extends string | number>({ value: current, options, onChange, label }: { value: T; options: [T, string][]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="flex rounded-full border border-line bg-panel p-0.5">
      {options.map(([v, text]) => (
        <button key={String(v)} role="radio" aria-checked={v === current} onClick={() => onChange(v)}
          className={`rounded-full px-3 py-1 text-xs font-medium transition ${v === current ? "bg-accent text-on-accent" : "text-ink-2 hover:text-ink"}`}>
          {text}
        </button>
      ))}
    </div>
  );
}

interface Tip { x: number; y: number; content: ReactNode }

function Tooltip({ tip }: { tip: Tip | null }) {
  if (!tip) return null;
  return (
    <div className="pointer-events-none fixed z-50 min-w-40 rounded-lg border border-line bg-panel px-3 py-2 text-xs shadow-[var(--shadow-pop)]"
      style={{ left: Math.min(tip.x + 14, window.innerWidth - 240), top: tip.y + 14 }}>
      {tip.content}
    </div>
  );
}

function Swatch({ color }: { color: string }) {
  return <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: color }} aria-hidden="true" />;
}

export default function Usage() {
  const [days, setDays] = useState(30);
  const [source, setSource] = useState("");
  const [metric, setMetric] = useState<Metric>("total");
  const [tip, setTip] = useState<Tip | null>(null);
  const data = useApi<UsageDaily>(`/usage/daily?days=${days}${source ? `&source=${source}` : ""}`);

  const view = useMemo((): View | null => {
    const d = data.data;
    if (!d) return null;
    // Colors follow the model, not its rank: slots go to models in name order, so a filter
    // that drops one model never repaints the others.
    const totals = new Map<string, number>();
    for (const r of d.rows) totals.set(r.model, (totals.get(r.model) ?? 0) + value(r, metric));
    const byName = [...totals.keys()].sort((a, b) => a.localeCompare(b));
    const folded = byName.length > SLOTS;
    const top = folded
      ? new Set([...byName].sort((a, b) => (totals.get(b) ?? 0) - (totals.get(a) ?? 0)).slice(0, SLOTS - 1))
      : new Set(byName);
    const series = byName.filter((m) => top.has(m));
    if (folded) series.push(OTHER);
    const color = new Map(series.map((m, i) => [m, `var(--series-${i + 1})`]));
    const key = (m: string) => (top.has(m) ? m : OTHER);

    const cells = new Map<string, Row>(); // `${series}|${day}`, every metric summed
    for (const r of d.rows) {
      const k = `${key(r.model)}|${r.day}`;
      const c = cells.get(k);
      cells.set(k, c ? {
        ...c,
        input_tokens: c.input_tokens + r.input_tokens,
        cached_input_tokens: c.cached_input_tokens + r.cached_input_tokens,
        output_tokens: c.output_tokens + r.output_tokens,
        requests: c.requests + r.requests,
      } : { ...r, model: key(r.model) });
    }
    const at = (m: string, day: string) => cells.get(`${m}|${day}`);
    const dayTotal = d.days.map((day) => series.reduce((s, m) => s + (at(m, day) ? value(at(m, day)!, metric) : 0), 0));
    const maxDay = Math.max(0, ...dayTotal);
    const maxCell = Math.max(0, ...[...cells.values()].map((c) => value(c, metric)));
    const seriesTotals = series.map((m) => {
      const rows = d.days.map((day) => at(m, day)).filter((r): r is Row => !!r);
      const sum = (f: (r: Row) => number) => rows.reduce((s, r) => s + f(r), 0);
      return {
        model: m,
        input: sum((r) => r.input_tokens), cached: sum((r) => r.cached_input_tokens),
        output: sum((r) => r.output_tokens), requests: sum((r) => r.requests),
        value: sum((r) => value(r, metric)), activeDays: rows.length,
      };
    });
    return { days: d.days, series, color, at, dayTotal, maxDay, maxCell, seriesTotals };
  }, [data.data, metric]);

  const metricName = METRICS.find(([m]) => m === metric)![1].toLowerCase();
  const labelEvery = days <= 7 ? 1 : days <= 30 ? 5 : 15;
  const empty = view && !view.series.length;

  return (
    <>
      <PageHeader title="Usage" subtitle="Tokens each model used per day, across tasks, Chat, standalone runs and direct API calls. Days are in your local time." />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Segmented label="Date range" value={days} onChange={setDays} options={RANGES.map((n) => [n, `${n} days`])} />
        <Segmented label="Source" value={source} onChange={setSource} options={SOURCES} />
        <Segmented label="Measure" value={metric} onChange={setMetric} options={METRICS} />
      </div>
      <ErrorNote error={data.error} />
      {empty && <Card><Empty>No usage in this range yet.</Empty></Card>}
      {view && !empty && (
        <div className="space-y-4">
          <Card title={`Heatmap · ${metricName}`}>
            <div className="overflow-x-auto overflow-y-hidden">
              <div className="grid min-w-[480px] items-center gap-y-1" style={{ gridTemplateColumns: `minmax(8rem, max-content) repeat(${view.days.length}, minmax(4px, 1fr))` }}>
                {view.series.map((m) => (
                  <HeatRow key={m} model={m} view={view} metric={metric} metricName={metricName} setTip={setTip} />
                ))}
                <div />
                {view.days.map((day, i) => (
                  <div key={day} className="relative h-4 text-[10px] text-muted">
                    {showLabel(i, view.days.length, labelEvery) && <span className="absolute left-0 top-0.5 whitespace-nowrap">{dayLabel(day)}</span>}
                  </div>
                ))}
              </div>
            </div>
            <div className="mt-3 flex items-center gap-2 text-xs text-muted">
              <span>Less</span>
              {[0.12, 0.3, 0.5, 0.75, 1].map((o) => <span key={o} className="h-3 w-3 rounded-sm" style={{ background: "var(--heat)", opacity: o }} aria-hidden="true" />)}
              <span>More (up to {fmt(view.maxCell)} {metricName} in a day)</span>
            </div>
          </Card>

          <Card title={`Per day · ${metricName}`} actions={<Legend view={view} />}>
            <Bars view={view} metric={metric} metricName={metricName} labelEvery={labelEvery} setTip={setTip} />
          </Card>

          <Card title="By model">
            <table className="w-full">
              <thead><tr>{["Model", "Input", "Cached input", "Cache hit", "Output", "Requests", "Active days"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
              <tbody>{[...view.seriesTotals].sort((a, b) => b.value - a.value).map((t) => (
                <tr key={t.model}>
                  <td className="td"><span className="flex items-center gap-2"><Swatch color={view.color.get(t.model)!} /><span className="mono text-xs">{t.model}</span></span></td>
                  <td className="td tabular-nums">{t.input.toLocaleString()}</td>
                  <td className="td tabular-nums">{t.cached.toLocaleString()}</td>
                  <td className="td tabular-nums">{t.input ? `${Math.round((t.cached / t.input) * 100)}%` : "—"}</td>
                  <td className="td tabular-nums">{t.output.toLocaleString()}</td>
                  <td className="td tabular-nums">{t.requests.toLocaleString()}</td>
                  <td className="td tabular-nums">{t.activeDays}</td>
                </tr>))}</tbody>
            </table>
          </Card>
        </div>
      )}
      <Tooltip tip={tip} />
    </>
  );
}

interface View {
  days: string[];
  series: string[];
  color: Map<string, string>;
  at: (model: string, day: string) => Row | undefined;
  dayTotal: number[];
  maxDay: number;
  maxCell: number;
  seriesTotals: { model: string; input: number; cached: number; output: number; requests: number; value: number; activeDays: number }[];
}

function Legend({ view }: { view: View }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1">
      {view.series.map((m) => <span key={m} className="flex items-center gap-1.5 text-xs text-ink-2"><Swatch color={view.color.get(m)!} /><span className="mono">{m}</span></span>)}
    </div>
  );
}

function HeatRow({ model, view, metric, metricName, setTip }: { model: string; view: View; metric: Metric; metricName: string; setTip: (t: Tip | null) => void }) {
  return (
    <>
      <div className="flex min-w-0 items-center gap-2 pr-3 text-xs"><Swatch color={view.color.get(model)!} /><span className="mono truncate" title={model}>{model}</span></div>
      {view.days.map((day) => {
        const r = view.at(model, day);
        const v = r ? value(r, metric) : 0;
        // A square-root scale keeps a quiet day visible next to a busy one.
        const opacity = v && view.maxCell ? 0.12 + 0.88 * Math.sqrt(v / view.maxCell) : 0;
        return (
          <div key={day} className="h-6 px-px"
            onMouseMove={(e) => setTip({ x: e.clientX, y: e.clientY, content: (
              <>
                <div className="mb-1 font-semibold">{dayLabel(day, true)}</div>
                <div className="mono mb-1 text-ink-2">{model}</div>
                <div className="tabular-nums">{v.toLocaleString()} {metricName}</div>
                {r && metric !== "requests" && <div className="tabular-nums text-muted">{r.requests} request{r.requests === 1 ? "" : "s"}</div>}
              </>
            ) })}
            onMouseLeave={() => setTip(null)}>
            <div className="h-full rounded-[3px] bg-panel-2">
              {v > 0 && <div className="h-full rounded-[3px]" style={{ background: "var(--heat)", opacity }} />}
            </div>
          </div>
        );
      })}
    </>
  );
}

function Bars({ view, metric, metricName, labelEvery, setTip }: { view: View; metric: Metric; metricName: string; labelEvery: number; setTip: (t: Tip | null) => void }) {
  const [hover, setHover] = useState<number | null>(null);
  const ticks = view.maxDay ? [view.maxDay, view.maxDay / 2] : [];
  return (
    <div className="flex gap-2">
      <div className="relative h-56 w-12 shrink-0 text-right text-[10px] text-muted">
        {ticks.map((t) => <span key={t} className="absolute right-0 -translate-y-1/2 tabular-nums" style={{ top: `${(1 - t / view.maxDay) * 100}%` }}>{fmt(Math.round(t))}</span>)}
        <span className="absolute bottom-0 right-0 translate-y-1/2">0</span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="relative h-56 border-b border-line-strong">
          {ticks.map((t) => <div key={t} className="absolute inset-x-0 border-t border-dashed border-line" style={{ top: `${(1 - t / view.maxDay) * 100}%` }} />)}
          <div className="absolute inset-0 flex items-end">
            {view.days.map((day, i) => {
              const total = view.dayTotal[i];
              return (
                <div key={day} className={`flex h-full min-w-0 flex-1 flex-col justify-end px-[1px] ${hover === i ? "bg-panel-2" : ""}`}
                  onMouseMove={(e) => { setHover(i); setTip({ x: e.clientX, y: e.clientY, content: (
                    <>
                      <div className="mb-1 font-semibold">{dayLabel(day, true)}</div>
                      {view.series.filter((m) => view.at(m, day)).map((m) => (
                        <div key={m} className="flex items-center gap-2"><Swatch color={view.color.get(m)!} /><span className="mono flex-1 truncate">{m}</span><span className="tabular-nums">{fmt(value(view.at(m, day)!, metric))}</span></div>
                      ))}
                      <div className="mt-1 border-t border-line pt-1 tabular-nums">{total ? `${total.toLocaleString()} ${metricName}` : `No ${metricName}`}</div>
                    </>
                  ) }); }}
                  onMouseLeave={() => { setHover(null); setTip(null); }}>
                  {total > 0 && (
                    <div className="mx-auto flex w-full max-w-10 flex-col-reverse gap-[2px] overflow-hidden rounded-t-[4px]" style={{ height: `${(total / view.maxDay) * 100}%` }}>
                      {view.series.map((m) => {
                        const r = view.at(m, day);
                        const v = r ? value(r, metric) : 0;
                        return v ? <div key={m} className="min-h-px" style={{ flexGrow: v, flexBasis: 0, background: view.color.get(m) }} /> : null;
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
        <div className="relative mt-1 flex h-4 text-[10px] text-muted">
          {view.days.map((day, i) => (
            <div key={day} className="relative min-w-0 flex-1">
              {showLabel(i, view.days.length, labelEvery) && <span className="absolute left-0 whitespace-nowrap">{dayLabel(day)}</span>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
