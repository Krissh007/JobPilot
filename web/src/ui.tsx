// Small shared UI pieces and formatters.
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { STAGE_COLOR, type Stage } from "./api";

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

type Variant = "default" | "primary" | "ghost" | "danger";
export function Button({ variant = "default", size = "md", className, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }) {
  return (
    <button
      {...props}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-md border font-medium whitespace-nowrap transition-colors disabled:opacity-45 disabled:cursor-not-allowed",
        size === "sm" ? "px-2 py-0.5 text-[12px]" : "px-3 py-1.5 text-[12.5px]",
        variant === "primary" && "bg-accent border-accent text-accent-ink hover:brightness-110",
        variant === "default" && "bg-surface border-line-2 text-fg hover:border-accent",
        variant === "ghost" && "bg-transparent border-transparent text-muted hover:text-fg hover:bg-sunk",
        variant === "danger" && "bg-surface border-bad/50 text-bad hover:bg-bad-soft",
        className,
      )}
    />
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="ml-1 rounded border border-current/30 px-1 font-mono text-[10.5px] leading-4 opacity-70">{children}</kbd>;
}

export function Segmented<T extends string>({ value, options, onChange, label }:
  { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="flex overflow-hidden rounded-md border border-line-2 bg-surface">
      {options.map((o) => (
        <button key={o.value} role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
          className={cx("px-2.5 py-1 text-[12.5px]", value === o.value ? "bg-sunk font-medium text-fg" : "text-muted hover:text-fg")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function StageDot({ stage, round = true }: { stage: Stage; round?: boolean }) {
  return <i className={cx("inline-block h-[7px] w-[7px] shrink-0", round ? "rounded-full" : "rounded-[2px]")} style={{ background: STAGE_COLOR[stage] }} />;
}

export function scoreTone(score: number | null | undefined, threshold: number) {
  if (score == null) return "text-muted";
  if (score >= threshold) return "text-good";
  if (score >= threshold - 15) return "text-warn";
  return "text-muted";
}

export function ScoreNum({ score, threshold, className }: { score: number | null; threshold: number; className?: string }) {
  return <span className={cx("num font-semibold", scoreTone(score, threshold), className)}>{score == null ? "—" : Math.round(score)}</span>;
}

export function SourceTag({ source }: { source: string }) {
  const short = source === "linkedin" ? "LI" : source === "naukri" ? "NK" : source.slice(0, 3).toUpperCase();
  return <span title={source} className="num text-[10.5px] font-medium tracking-wide text-muted">{short}</span>;
}

export function ExampleTag() {
  return <span className="rounded border border-dashed border-line-2 px-1 text-[10.5px] text-muted">example</span>;
}

/** Times come from the API as UTC; everything is shown in IST. */
const IST = "Asia/Kolkata";
export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { timeZone: IST, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}
export function duration(start: string, end: string | null): string {
  if (!end) return "—";
  const s = Math.max(0, Math.round((new Date(end).getTime() - new Date(start).getTime()) / 1000));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}
export function expRange(min: number | null, max: number | null): string {
  if (min == null && max == null) return "—";
  return `${min ?? "?"}–${max ?? "?"}y`;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line-2 bg-surface px-6 py-10 text-center text-muted">
      <div className="mb-1 font-display text-[15px] font-semibold text-fg">{title}</div>
      {children}
    </div>
  );
}

export function Panel({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx("overflow-hidden rounded-lg border border-line bg-surface", className)}>
      {title != null && (
        <header className="flex items-center gap-2 border-b border-line bg-sunk/60 px-3.5 py-2">
          <h3 className="mr-auto text-[12.5px] font-semibold">{title}</h3>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export const inputClass = "w-full rounded-md border border-line-2 bg-bg px-2.5 py-1.5 text-[13px] text-fg placeholder:text-muted focus:border-accent focus:outline-none";
export const labelClass = "mb-1 block text-[11px] font-semibold uppercase tracking-[.07em] text-muted";
