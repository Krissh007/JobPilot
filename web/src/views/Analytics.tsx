import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { STAGE_LABEL, type Stage } from "../api";
import { useFunnel, usePerDay, useScoreHistogram } from "../data";
import { Button, Empty, Panel, Segmented, cx } from "../ui";

/* Charts follow the dataviz method: one hue for magnitude (--chart), emphasis vs a
   de-emphasis gray (--chart-dim, validated in both themes), thin marks with 4px rounded
   data-ends, hairline grids, a hover tooltip on every mark and a table view per chart. */

type Range = "30" | "90";
type Tip = { x: number; y: number; value: string; label: string } | null;

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

/** Clean axis ticks for small counts: 0..max in 1/2/5×10ⁿ steps, at most ~4 lines. */
function ticks(max: number): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? pow * 10);
  const top = Math.max(step, Math.ceil(max / step) * step);
  return Array.from({ length: Math.round(top / step) + 1 }, (_, i) => Math.round(i * step));
}

const pct = (part: number, whole: number) => (whole ? `${Math.round((part / whole) * 100)}%` : "—");
const dayLabel = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

/** Rounded data-end, square at the baseline (columns grow up from y0). */
function columnPath(x: number, y: number, w: number, y0: number, r = 4): string {
  const h = y0 - y;
  if (h <= 0) return "";
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y0} V${y + rr} Q${x},${y} ${x + rr},${y} H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr} V${y0} Z`;
}

function Tooltip({ tip }: { tip: Tip }) {
  if (!tip) return null;
  return (
    <div role="status" className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded-md border border-line-2 bg-surface px-2 py-1 text-[12px] shadow-md"
      style={{ left: tip.x, top: tip.y - 6 }}>
      <div className="num font-semibold text-fg">{tip.value}</div>
      <div className="text-muted">{tip.label}</div>
    </div>
  );
}

function ChartPanel({ title, subtitle, table, children, busy }: { title: string; subtitle?: ReactNode; table: ReactNode; children: ReactNode; busy: boolean }) {
  const [asTable, setAsTable] = useState(false);
  return (
    <Panel title={title} actions={<Button size="sm" variant="ghost" aria-pressed={asTable} onClick={() => setAsTable(!asTable)}>{asTable ? "Chart" : "Table"}</Button>}>
      <div className={cx("p-3.5 transition-opacity", busy && "opacity-60")}>
        {subtitle && <p className="m-0 mb-2 text-[12px] text-muted">{subtitle}</p>}
        {asTable ? table : children}
      </div>
    </Panel>
  );
}

function DataTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <div className="max-h-72 overflow-auto rounded-md border border-line">
      <table className="w-full border-collapse text-[12.5px]">
        <thead className="sticky top-0"><tr className="border-b border-line bg-sunk text-left text-[11px] uppercase tracking-[.06em] text-muted">
          {head.map((h, i) => <th key={h} className={cx("px-2.5 py-1.5", i > 0 && "text-right")}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={i} className="border-b border-line last:border-0">{r.map((c, j) => <td key={j} className={cx("px-2.5 py-1", j > 0 && "num text-right")}>{c}</td>)}</tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Stat({ label, value, note }: { label: string; value: string | number; note?: string }) {
  return (
    <div className="rounded-lg border border-line bg-surface px-3.5 py-3">
      <div className="text-[12px] text-muted">{label}</div>
      <div className="mt-0.5 text-[26px] font-semibold leading-tight text-fg">{value}</div>
      {note && <div className="text-[11.5px] text-muted">{note}</div>}
    </div>
  );
}

/* ---------------- Funnel: horizontal bars, one hue ---------------- */
function FunnelChart({ stages }: { stages: { stage: Stage; count: number }[] }) {
  const [tip, setTip] = useState<Tip>(null);
  const top = stages[0]?.count || 1;
  const info = (s: { stage: Stage; count: number }) => ({ value: `${s.count} jobs`, label: `${STAGE_LABEL[s.stage]} · ${pct(s.count, top)} of found` });
  return (
    <div className="relative" onPointerLeave={() => setTip(null)}>
      <div className="flex flex-col gap-2">
        {stages.map((s) => (
          <div key={s.stage} className="grid grid-cols-[84px_1fr_76px] items-center gap-2.5 text-[12.5px]"
            tabIndex={0} aria-label={`${STAGE_LABEL[s.stage]}: ${s.count} (${pct(s.count, top)} of found)`}
            onPointerMove={(e) => { const p = e.currentTarget.parentElement!.getBoundingClientRect(); setTip({ x: e.clientX - p.left, y: e.clientY - p.top, ...info(s) }); }}
            onFocus={(e) => { const r = e.currentTarget.getBoundingClientRect(), p = e.currentTarget.parentElement!.getBoundingClientRect(); setTip({ x: r.left - p.left + r.width / 2, y: r.top - p.top, ...info(s) }); }}
            onBlur={() => setTip(null)}>
            <span className="text-muted">{STAGE_LABEL[s.stage]}</span>
            <div className="rounded-sm py-0.5 hover:bg-sunk/70">
              {s.count > 0 && <div className="h-[14px] rounded-r-[4px]" style={{ width: `max(${(s.count / top) * 100}%, 3px)`, background: "var(--chart)" }} />}
              {s.count === 0 && <div className="h-[14px]" />}
            </div>
            <span className="num text-right"><b className="font-semibold">{s.count}</b> <span className="text-muted">{pct(s.count, top)}</span></span>
          </div>
        ))}
      </div>
      <Tooltip tip={tip} />
    </div>
  );
}

/* ---------------- Columns (histogram and per-day) ---------------- */
interface Column { key: string; label: string; axis: string; value: number; emphasis: boolean; tip: string }

function ColumnChart({ columns, height = 190, marker, axisEvery = 1 }:
  { columns: Column[]; height?: number; marker?: { at: number; label: string }; axisEvery?: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip>(null);
  const [hover, setHover] = useState<string | null>(null);
  const left = 28, right = 6, top = 18, bottom = 22;
  const max = Math.max(0, ...columns.map((c) => c.value));
  const ys = ticks(max);
  const yMax = ys[ys.length - 1] || 1;
  const plotW = Math.max(0, width - left - right), plotH = height - top - bottom;
  const slot = columns.length ? plotW / columns.length : 0;
  const barW = Math.max(2, Math.min(24, slot - 2));
  const every = Math.max(axisEvery, Math.ceil(58 / Math.max(slot, 1)));  // keep axis labels ~58px apart on narrow screens
  const y = (v: number) => top + plotH - (v / yMax) * plotH;
  const peak = columns.reduce<Column | null>((best, c) => (c.value > (best?.value ?? 0) ? c : best), null);
  const peakIndex = peak ? columns.indexOf(peak) : -1;
  const show = (c: Column, i: number) => { setHover(c.key); setTip({ x: left + slot * i + slot / 2, y: y(c.value), value: c.tip, label: c.label }); };
  const hide = () => { setTip(null); setHover(null); };

  return (
    <div ref={ref} className="relative" onPointerLeave={hide}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={`Column chart, ${columns.length} columns, peak ${peak?.value ?? 0}`} className="block overflow-visible">
          {ys.map((t) => (
            <g key={t}>
              <line x1={left} x2={width - right} y1={y(t)} y2={y(t)} stroke="var(--line)" strokeWidth={1} />
              <text x={left - 6} y={y(t)} dy="0.32em" textAnchor="end" className="num" fontSize={10.5} fill="var(--muted)">{t}</text>
            </g>
          ))}
          <line x1={left} x2={width - right} y1={y(0)} y2={y(0)} stroke="var(--line-2)" strokeWidth={1} />
          {columns.map((c, i) => (
            <g key={c.key}>
              <path d={columnPath(left + slot * i + (slot - barW) / 2, y(c.value), barW, y(0))}
                fill={c.emphasis ? "var(--chart)" : "var(--chart-dim)"} opacity={hover && hover !== c.key ? 0.7 : 1} />
              {i % every === 0 && <text x={left + slot * i + slot / 2} y={height - 6} textAnchor="middle" fontSize={10.5} fill="var(--muted)" className="num">{c.axis}</text>}
            </g>
          ))}
          {peak && peak.value > 0 && (
            <text x={left + slot * peakIndex + slot / 2} y={y(peak.value) - 5} textAnchor="middle" fontSize={11} fontWeight={600} fill="var(--fg)" className="num">{peak.value}</text>
          )}
          {marker && (
            <g>
              <line x1={left + (marker.at / 100) * plotW} x2={left + (marker.at / 100) * plotW} y1={top - 4} y2={top + plotH} stroke="var(--fg)" strokeWidth={1} opacity={0.55} />
              <text x={left + (marker.at / 100) * plotW + 4} y={top - 6} fontSize={10.5} fill="var(--muted)">{marker.label}</text>
            </g>
          )}
          {/* hit targets: the whole slot, taller than the mark; drawn last so they sit on top */}
          {columns.map((c, i) => (
            <rect key={`hit-${c.key}`} x={left + slot * i} y={top} width={slot} height={plotH} fill="transparent" tabIndex={0}
              aria-label={`${c.label}: ${c.tip}`} onPointerMove={() => show(c, i)} onFocus={() => show(c, i)} onBlur={hide} />
          ))}
        </svg>
      )}
      <Tooltip tip={tip} />
    </div>
  );
}

function Legend({ items }: { items: { color: string; label: string }[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted">
      {items.map((it) => <span key={it.label} className="inline-flex items-center gap-1.5"><i className="inline-block h-2.5 w-2.5 rounded-[2px]" style={{ background: it.color }} />{it.label}</span>)}
    </div>
  );
}

export function Analytics() {
  const [range, setRange] = useState<Range>("30");
  const [examples, setExamples] = useState(false);
  const funnel = useFunnel(examples);
  const scores = useScoreHistogram(examples);
  const perDay = usePerDay(Number(range), examples);

  if (!funnel.data || !scores.data || !perDay.data) return <Empty title="Loading analytics…" />;
  const stages = funnel.data.stages;
  const count = (s: Stage) => stages.find((x) => x.stage === s)?.count ?? 0;
  const days = perDay.data.days;
  const appliedInRange = days.reduce((n, d) => n + d.count, 0);
  const last7 = days.slice(-7).reduce((n, d) => n + d.count, 0);
  const threshold = scores.data.threshold;
  const bins = scores.data.bins;
  const scored = bins.reduce((n, b) => n + b.count, 0);
  const above = bins.filter((b) => b.from >= threshold).reduce((n, b) => n + b.count, 0);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented label="Date range" value={range} onChange={setRange} options={[{ value: "30", label: "Last 30 days" }, { value: "90", label: "Last 90 days" }]} />
        <label className="flex items-center gap-1.5 text-[12.5px] text-muted">
          <input type="checkbox" checked={examples} onChange={(e) => setExamples(e.target.checked)} className="accent-[var(--accent)]" /> Include example rows
        </label>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Jobs tracked" value={funnel.data.total} note={`${funnel.data.rejected} rejected`} />
        <Stat label="Shortlisted" value={count("shortlisted")} note={`${pct(count("shortlisted"), count("found"))} of jobs found`} />
        <Stat label={`Applied, last ${range} days`} value={appliedInRange} note={`${last7} in the last 7 days`} />
        <Stat label="Interviews" value={count("interview")} note={`${pct(count("interview"), count("applied"))} of applications`} />
      </div>

      <div className="grid gap-3 lg:grid-cols-2 [&>*]:min-w-0">
        <ChartPanel title="Pipeline funnel" busy={funnel.isFetching && funnel.isPlaceholderData}
          subtitle="Jobs that ever reached each stage (from status history), as a share of jobs found."
          table={<DataTable head={["Stage", "Jobs", "% of found"]} rows={stages.map((s) => [STAGE_LABEL[s.stage], s.count, pct(s.count, count("found"))])} />}>
          <FunnelChart stages={stages} />
        </ChartPanel>

        <ChartPanel title="Fit scores" busy={scores.isFetching && scores.isPlaceholderData}
          subtitle={<><b className="num font-semibold text-fg">{above}</b> of {scored} scored jobs are at or above the shortlist threshold ({threshold}){scores.data.unscored ? ` · ${scores.data.unscored} not scored yet` : ""}.</>}
          table={<DataTable head={["Score", "Jobs"]} rows={bins.map((b) => [`${b.from}–${b.to}`, b.count])} />}>
          <ColumnChart marker={{ at: threshold, label: `threshold ${threshold}` }}
            columns={bins.map((b) => ({ key: String(b.from), label: `score ${b.from}–${b.to}`, axis: String(b.from), value: b.count,
              emphasis: b.from >= threshold, tip: `${b.count} job${b.count === 1 ? "" : "s"}` }))} />
          <Legend items={[{ color: "var(--chart)", label: `At or above threshold (${threshold})` }, { color: "var(--chart-dim)", label: "Below threshold" }]} />
        </ChartPanel>
      </div>

      <ChartPanel title="Applications per day" busy={perDay.isFetching && perDay.isPlaceholderData}
        subtitle={`Applications marked applied each day (IST), last ${range} days.`}
        table={<DataTable head={["Date", "Applications"]} rows={[...days].reverse().map((d) => [dayLabel(d.date), d.count])} />}>
        <ColumnChart height={170} axisEvery={range === "30" ? 5 : 15}
          columns={days.map((d) => ({ key: d.date, label: dayLabel(d.date), axis: dayLabel(d.date), value: d.count, emphasis: true,
            tip: `${d.count} application${d.count === 1 ? "" : "s"}` }))} />
      </ChartPanel>
    </div>
  );
}
