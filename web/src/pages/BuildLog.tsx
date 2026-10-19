import { useState } from "react";
import { Link } from "react-router-dom";
import type { BuildLog as Entry } from "../api/types";
import { Card, ErrorNote, PageHeader, fmtTime } from "../components/ui";
import { useApi } from "../hooks/useApi";

export default function BuildLog() {
  const [limit, setLimit] = useState(200);
  const [filter, setFilter] = useState("");
  const { data, error } = useApi<Entry[]>(`/build-log?limit=${limit}`, 5000);
  const rows = (data ?? []).filter((e) => !filter || `${e.event} ${e.details} ${e.role} ${e.phase}`.toLowerCase().includes(filter.toLowerCase()));
  return (
    <>
      <PageHeader title="Build log" subtitle="Machine-readable log mirrored to ORCHESTRATOR_HOME/build-log.jsonl." />
      <ErrorNote error={error} />
      <Card actions={<>
        <input className="input w-64" placeholder="filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <select className="input w-auto" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>{[100, 200, 500, 2000].map((n) => <option key={n}>{n}</option>)}</select>
      </>} title={`${rows.length} entries`}>
        <table className="w-full">
          <thead><tr>{["Time", "Phase", "Task", "Run", "Role", "Event", "Details", "Commit"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{rows.map((e) => (
            <tr key={e.id}>
              <td className="td whitespace-nowrap text-xs text-muted">{fmtTime(e.timestamp)}</td>
              <td className="td text-xs">{e.phase ?? "—"}</td>
              <td className="td text-xs">{e.task_id ? <Link className="text-accent" to={`/tasks/${e.task_id}`}>T-{e.task_id}</Link> : "—"}</td>
              <td className="td text-xs">{e.run_id ? `R-${e.run_id}` : "—"}</td>
              <td className="td text-xs">{e.role}</td>
              <td className="td mono text-xs text-accent">{e.event}</td>
              <td className="td text-xs">{e.details}</td>
              <td className="td mono text-xs text-muted">{e.commit_sha?.slice(0, 8) ?? ""}</td>
            </tr>))}</tbody>
        </table>
      </Card>
    </>
  );
}
