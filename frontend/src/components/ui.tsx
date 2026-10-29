import { useEffect, useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Priority } from "../api/types";
import { priorityLabel } from "../lib/format";
import { subscribeToasts, type Toast } from "../lib/ui-events";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-white hover:bg-accent-strong disabled:opacity-50",
  secondary: "bg-surface text-ink border border-line hover:bg-bg disabled:opacity-50",
  danger: "bg-danger text-white hover:opacity-90 disabled:opacity-50",
  ghost: "text-muted hover:bg-black/5 disabled:opacity-50",
};

export function Button({
  variant = "primary",
  loading,
  className = "",
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; loading?: boolean }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={`inline-flex h-9 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors ${variants[variant]} ${className}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

export function Spinner() {
  return <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

export function Field({
  label,
  hint,
  error,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string; error?: string }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{label}</span>
      <input
        {...rest}
        className="h-9 w-full rounded-md border border-line bg-surface px-3 outline-none focus:border-accent"
      />
      {hint && !error && <span className="mt-1 block text-xs text-muted">{hint}</span>}
      {error && <span className="mt-1 block text-xs text-danger">{error}</span>}
    </label>
  );
}

export function Select({
  label,
  className = "",
  children,
  ...rest
}: React.SelectHTMLAttributes<HTMLSelectElement> & { label?: string }) {
  const el = (
    <select
      {...rest}
      className={`h-9 rounded-md border border-line bg-surface px-2 text-sm outline-none focus:border-accent ${className}`}
    >
      {children}
    </select>
  );
  return label ? (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{label}</span>
      {el}
    </label>
  ) : (
    el
  );
}

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:p-10" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
        className={`w-full rounded-xl bg-surface shadow-xl ${wide ? "max-w-3xl" : "max-w-md"}`}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-base font-semibold">{title}</h2>
          <button aria-label="Закрыть" className="rounded p-1 text-muted hover:bg-black/5" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return <div className={`skeleton ${className}`} />;
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-line bg-surface px-6 py-12 text-center">
      <p className="font-medium">{title}</p>
      {hint && <p className="mt-1 text-sm text-muted">{hint}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-danger/30 bg-danger/5 px-4 py-3 text-sm text-danger">
      {message}
      {onRetry && (
        <button className="ml-3 underline" onClick={onRetry}>
          Повторить
        </button>
      )}
    </div>
  );
}

const bannerTone = {
  info: "border-accent/30 bg-accent-soft text-ink",
  warn: "border-warn/40 bg-amber-50 text-ink",
  danger: "border-danger/40 bg-red-50 text-ink",
} as const;

export function Banner({
  tone,
  children,
  testId,
}: {
  tone: keyof typeof bannerTone;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <div data-testid={testId} className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-sm ${bannerTone[tone]}`}>
      {children}
    </div>
  );
}

const avatarColors = ["#5b5bd6", "#2f855a", "#b7791f", "#c05621", "#2b6cb0", "#97266d", "#2c7a7b"];

export function Avatar({ name, size = 24 }: { name: string; size?: number }) {
  const initials = name
    .split(/\s+/)
    .map((p) => p[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
  const color = avatarColors[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % avatarColors.length];
  return (
    <span
      title={name}
      style={{ width: size, height: size, background: color, fontSize: size * 0.42 }}
      className="inline-flex shrink-0 items-center justify-center rounded-full font-semibold text-white"
    >
      {initials}
    </span>
  );
}

const prioColor: Record<Priority, string> = {
  low: "bg-slate-100 text-slate-600",
  medium: "bg-blue-50 text-blue-700",
  high: "bg-amber-50 text-amber-700",
  urgent: "bg-red-50 text-red-700",
};

export function PriorityBadge({ priority }: { priority: Priority }) {
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${prioColor[priority]}`}>{priorityLabel[priority]}</span>;
}

export function Toasts() {
  const [items, setItems] = useState<Toast[]>([]);
  useEffect(
    () =>
      subscribeToasts((t) => {
        setItems((cur) => [...cur, t]);
        setTimeout(() => setItems((cur) => cur.filter((x) => x.id !== t.id)), 5000);
      }),
    [],
  );
  return createPortal(
    <div className="fixed bottom-4 right-4 z-[60] flex max-w-[calc(100vw-2rem)] flex-col gap-2" aria-live="polite">
      {items.map((t) => (
        <div
          key={t.id}
          role="status"
          className={`rounded-lg px-4 py-2.5 text-sm text-white shadow-lg ${t.kind === "error" ? "bg-danger" : "bg-ink"}`}
        >
          {t.text}
        </div>
      ))}
    </div>,
    document.body,
  );
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="rounded-xl border border-line bg-surface">
      {(title || actions) && (
        <header className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="font-semibold">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}
