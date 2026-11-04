import { Link } from "react-router-dom";
import type { NotificationRecord } from "../api/types";
import { Badge, Card, ErrorNote, PageHeader, fmtTime } from "../components/ui";
import { useApi } from "../hooks/useApi";

export default function Notifications() {
  const { data, error } = useApi<NotificationRecord[]>("/notifications?limit=300", 5000);
  const rows = data ?? [];
  return (
    <>
      <PageHeader title="Notifications" subtitle="Durable record of every notification decision. Desktop delivery is best-effort; this list is not." />
      <ErrorNote error={error} />
      <Card title={`${rows.length} recent notifications`}>
        <table className="w-full">
          <thead><tr>{["Time", "Status", "Trigger", "Task / Run", "Notification"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{rows.map((n) => (
            <tr key={n.id}>
              <td className="td whitespace-nowrap text-xs text-muted">{fmtTime(n.created_at)}</td>
              <td className="td"><Badge value={n.status} /></td>
              <td className="td mono text-xs text-accent">{n.trigger}</td>
              <td className="td text-xs">{n.task_id ? <Link className="text-accent" to={`/tasks/${n.task_id}`}>T-{n.task_id}</Link> : "—"}{n.run_id ? <span className="text-muted"> · R-{n.run_id}</span> : null}</td>
              <td className="td text-xs"><div className="font-medium">{n.title}</div><div className="whitespace-pre-wrap text-muted">{n.body}</div>{n.reason && <div className="text-muted">reason: {n.reason}</div>}{n.error && <div className="text-bad">error: {n.error}</div>}{n.link && <a className="text-accent underline" href={n.link}>Open task</a>}</td>
            </tr>))}</tbody>
        </table>
      </Card>
    </>
  );
}
