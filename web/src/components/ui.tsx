import { useEffect, useState, type ReactNode } from "react";

const TONE: Record<string, string> = {
  accepted: "ok", completed: "ok", pass: "ok", APPROVED: "ok", approved: "ok", merged: "ok", ready: "ok",
  queued: "info", planned: "muted", draft: "muted", archived: "muted", pending: "warn",
  in_progress: "warn", running: "warn", preparing: "warn", testing: "warn", review: "info",
  blocked: "bad", failed: "bad", rejected: "bad", timeout: "bad", cancelled: "muted", fail: "bad",
  CHANGES_REQUESTED: "warn", NEEDS_HUMAN: "bad", not_applicable: "muted", skipped: "muted",
  high: "bad", medium: "warn", low: "muted",
};
const TONE_CLASS: Record<string, string> = {
  ok: "border-ok/40 bg-ok/10 text-ok",
  warn: "border-warn/40 bg-warn/10 text-warn",
  bad: "border-bad/40 bg-bad/10 text-bad",
  info: "border-info/40 bg-info/10 text-info",
  muted: "border-line bg-panel-2 text-muted",
};

export function Badge({ value, tone }: { value: string | null | undefined; tone?: string }) {
  if (!value) return <span className="text-muted">—</span>;
  const t = tone ?? TONE[value] ?? "muted";
  return (
    <span className={`inline-block whitespace-nowrap rounded border px-1.5 py-0.5 text-xs font-medium ${TONE_CLASS[t]}`}>
      {value.replace(/_/g, " ")}
    </span>
  );
}

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="text-sm font-semibold">{title}</h2>
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">{actions}</div>
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return <div className="mb-3 rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">{error}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-6 text-center text-sm text-muted">{children}</div>;
}

export function Pre({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <pre className={`mono max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-bg p-3 text-xs leading-relaxed ${className}`}>
      {children}
    </pre>
  );
}

/** Confirmation dialog for destructive actions. */
export function ConfirmButton({ label, message, onConfirm, danger = true, disabled }: {
  label: string; message: string; onConfirm: () => void; danger?: boolean; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  return (
    <>
      <button className={`btn ${danger ? "btn-danger" : ""}`} disabled={disabled} onClick={() => setOpen(true)}>
        {label}
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setOpen(false)}>
          <div role="dialog" aria-modal="true" className="card w-full max-w-md p-5" onClick={(e) => e.stopPropagation()}>
            <h3 className="mb-2 font-semibold">{label}?</h3>
            <p className="mb-5 text-sm text-muted">{message}</p>
            <div className="flex justify-end gap-2">
              <button className="btn" onClick={() => setOpen(false)} autoFocus>Cancel</button>
              <button className={`btn ${danger ? "btn-danger" : "btn-primary"}`} onClick={() => { setOpen(false); onConfirm(); }}>
                {label}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)}
      className="inline-flex items-center gap-2 text-sm">
      <span className={`relative h-5 w-9 rounded-full border transition ${checked ? "border-accent bg-accent/40" : "border-line bg-panel-2"}`}>
        <span className={`absolute top-0.5 h-3.5 w-3.5 rounded-full bg-ink transition-all ${checked ? "left-4.5" : "left-0.5"}`} />
      </span>
      {label}
    </button>
  );
}

export function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(s);
  return isNaN(d.getTime()) ? s : d.toLocaleString();
}

export function lines(s: string): string[] {
  return s.split("\n").map((l) => l.trim()).filter(Boolean);
}
