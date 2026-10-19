import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { getToken, setToken } from "../api/client";

const NAV = [
  ["/", "Dashboard"],
  ["/tasks", "Tasks"],
  ["/projects", "Projects"],
  ["/models", "Models & Providers"],
  ["/adapters", "Adapters"],
  ["/memory", "Memory"],
  ["/prompt-preview", "Prompt Cache"],
  ["/schedules", "Schedules"],
  ["/build-log", "Build Log"],
  ["/settings", "Settings"],
] as const;

export default function Layout({ children }: { children: ReactNode }) {
  const [needToken, setNeedToken] = useState(false);
  const [draft, setDraft] = useState(getToken());

  useEffect(() => {
    const onUnauthorized = () => setNeedToken(true);
    window.addEventListener("orchestrator:unauthorized", onUnauthorized);
    return () => window.removeEventListener("orchestrator:unauthorized", onUnauthorized);
  }, []);

  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-panel">
        <div className="px-4 py-4">
          <div className="text-sm font-semibold tracking-wide">coding-orchestrator</div>
          <div className="text-xs text-muted">local control plane</div>
        </div>
        <nav className="flex-1 space-y-0.5 px-2">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"}
              className={({ isActive }) =>
                `block rounded-md px-3 py-1.5 text-sm ${isActive ? "bg-accent/15 text-accent" : "text-muted hover:bg-panel-2 hover:text-ink"}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-3 text-xs text-muted">127.0.0.1 · no telemetry</div>
      </aside>
      <main className="min-w-0 flex-1 overflow-auto px-8 py-6">{children}</main>

      {needToken && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <form className="card w-full max-w-sm p-5" onSubmit={(e) => { e.preventDefault(); setToken(draft.trim()); setNeedToken(false); location.reload(); }}>
            <h3 className="mb-1 font-semibold">Token required</h3>
            <p className="mb-3 text-sm text-muted">This server has ORCHESTRATOR_TOKEN set. Enter it to continue.</p>
            <input className="input mb-4" type="password" value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus />
            <div className="flex justify-end"><button className="btn btn-primary" type="submit">Save</button></div>
          </form>
        </div>
      )}
    </div>
  );
}
