import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, STAGES, STAGE_COLOR, STAGE_LABEL, type Stage } from "../api";
import { useApplications, useMeta } from "../data";
import { TABS, useUI, type SourceFilter, type Theme } from "../state";
import { Button, Kbd, Segmented, StageDot, cx } from "../ui";

export function Header() {
  const { data: meta, isError } = useMeta();
  const { openJob, setTab, theme, setTheme, toast } = useUI();
  const qc = useQueryClient();
  const search = useMutation({
    mutationFn: () => api.startRun("search", { then_score: true }),
    onSuccess: (run) => { toast(`Search run #${run.id} started (then scoring). Follow it in Runs.`); setTab("runs"); qc.invalidateQueries({ queryKey: ["runs"] }); },
    onError: (e: Error) => toast(e.message),
  });
  const running = meta?.running ?? [];
  const nextTheme: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };

  return (
    <header className="flex h-10 items-center gap-3">
      <div className="mr-auto flex min-w-0 items-center gap-3">
        <span className="grid h-6 w-6 shrink-0 place-items-center rounded-md bg-accent font-mono text-[11px] font-bold text-accent-ink">JP</span>
        <h1 className="font-display text-[19px] font-bold tracking-tight">Job<span className="text-accent">Pilot</span></h1>
        <small className="hidden truncate border-l border-line-2 pl-3 text-muted md:block">AI BA · AI Product Analyst · Hyderabad · 2–4 yrs</small>
      </div>
      <span className="hidden items-center gap-1.5 text-[12px] text-muted sm:flex">
        <i className={cx("h-1.5 w-1.5 rounded-full", isError ? "bg-bad" : "bg-good")} />
        {isError ? "API offline" : <>DB live · <span className="num">{meta?.total_jobs ?? "…"}</span> jobs</>}
        {running.length > 0 && <span className="ml-1 rounded bg-accent-soft px-1.5 text-accent">{running.map((r) => r.agent).join(", ")} running</span>}
      </span>
      <Button variant="ghost" size="sm" onClick={() => setTheme(nextTheme[theme])} title="Theme: system → light → dark" aria-label={`Theme: ${theme}`}>
        {theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"} <span className="hidden lg:inline">{theme}</span>
      </Button>
      <Button onClick={() => search.mutate()} disabled={search.isPending || running.some((r) => r.agent === "search")}>Run search</Button>
      <Button variant="primary" onClick={() => openJob("new")}>Add job<Kbd>N</Kbd></Button>
    </header>
  );
}

export function Rail() {
  const { data: meta } = useMeta();
  const { filters, setFilters, toast } = useUI();
  const qc = useQueryClient();
  const [threshold, setThreshold] = useState<number | null>(null);
  const save = useMutation({
    mutationFn: (value: number) => api.saveSettings({ threshold: value }),
    onSuccess: (s) => { toast(`Shortlist threshold set to ${s.threshold}`); qc.invalidateQueries({ queryKey: ["meta"] }); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e: Error) => toast(e.message),
    onSettled: () => setThreshold(null),
  });
  const shown = threshold ?? meta?.threshold ?? 60;
  const total = meta ? Object.values(meta.stages).reduce((a, b) => a + b, 0) : 0;
  const cap = meta?.daily_apply_cap ?? 10;

  return (
    <section aria-label="Pipeline" className="grid grid-cols-1 gap-5 rounded-lg border border-line bg-surface px-4 py-3 lg:grid-cols-[1fr_300px]">
      <div className="min-w-0">
        <div className="grid grid-cols-4 gap-y-2 sm:grid-cols-7">
          {STAGES.map((stage, i) => {
            const on = filters.stage === stage;
            return (
              <button key={stage} aria-pressed={on} onClick={() => setFilters({ stage: on ? null : stage })}
                className={cx("rounded-sm px-3 text-left hover:bg-sunk", i > 0 && "sm:border-l sm:border-line", on && "bg-accent-soft")}>
                <div className={cx("num text-[22px] font-semibold leading-tight tracking-tight", on && "text-accent")}>{meta?.stages[stage] ?? "–"}</div>
                <div className="flex items-center gap-1.5 text-[11.5px] text-muted"><StageDot stage={stage} />{STAGE_LABEL[stage]}</div>
              </button>
            );
          })}
        </div>
        <div className="mt-3 flex h-1.5 overflow-hidden rounded-full bg-sunk" aria-hidden>
          {total > 0 && STAGES.map((s) => (meta!.stages[s] > 0 ? <i key={s} style={{ width: `${(meta!.stages[s] / total) * 100}%`, background: s === "found" ? "var(--line-2)" : STAGE_COLOR[s] }} /> : null))}
        </div>
      </div>
      <div className="flex flex-col justify-center gap-1.5 lg:border-l lg:border-line lg:pl-5">
        <div className="flex items-center justify-between"><label htmlFor="threshold" className="text-[11.5px] text-muted">Shortlist threshold</label><span className="num">{shown}</span></div>
        <input id="threshold" type="range" min={30} max={95} value={shown} className="h-3.5 w-full accent-[var(--accent)]"
          onChange={(e) => setThreshold(Number(e.target.value))}
          onPointerUp={() => threshold != null && threshold !== meta?.threshold && save.mutate(threshold)}
          onKeyUp={() => threshold != null && threshold !== meta?.threshold && save.mutate(threshold)} />
        <div className="flex items-center justify-between"><span className="text-[11.5px] text-muted">Applied today</span><span className="num">{meta?.applied_today ?? 0} / {cap}</span></div>
        <div className="flex gap-[3px]" aria-label={`${meta?.applied_today ?? 0} of ${cap} applications today`}>
          {Array.from({ length: cap }, (_, i) => <i key={i} className={cx("h-1.5 flex-1 rounded-[2px]", i < (meta?.applied_today ?? 0) ? "bg-accent" : "bg-sunk")} />)}
        </div>
      </div>
    </section>
  );
}

export function Tabs() {
  const { tab, setTab } = useUI();
  const { data: apps } = useApplications();
  const pending = apps?.filter((a) => a.state === "pending_review").length ?? 0;
  const blocked = apps?.filter((a) => a.state === "blocked").length ?? 0;
  return (
    <nav role="tablist" className="flex gap-0.5 overflow-x-auto">
      {TABS.map(([key, label]) => (
        <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}
          className={cx("-mb-px whitespace-nowrap border-b-2 px-2.5 py-2 font-medium", tab === key ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg")}>
          {label}
          {key === "review" && pending > 0 && <span className="num ml-1.5 rounded bg-warn-soft px-1.5 text-[10.5px] font-semibold text-warn">{pending}</span>}
          {key === "review" && blocked > 0 && <span className="num ml-1 rounded bg-bad-soft px-1.5 text-[10.5px] font-semibold text-bad" title="blocked">{blocked}</span>}
        </button>
      ))}
    </nav>
  );
}

export function Toolbar({ threshold }: { threshold: number }) {
  const { filters, setFilters } = useUI();
  const [q, setQ] = useState(filters.q);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { const t = window.setTimeout(() => setFilters({ q }), 250); return () => window.clearTimeout(t); }, [q, setFilters]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && (e.target.closest("input, textarea, select, [contenteditable]") != null);
      if (e.key === "/" && !typing) { e.preventDefault(); input.current?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="flex flex-wrap items-center gap-2 pb-2">
      <label className="flex min-w-[220px] flex-1 items-center gap-1.5 rounded-md border border-line-2 bg-surface px-2.5 py-1 text-muted focus-within:border-accent sm:flex-none sm:w-[300px]">
        <span aria-hidden>⌕</span>
        <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search title, company, JD…" aria-label="Search jobs"
          className="min-w-0 flex-1 bg-transparent text-fg placeholder:text-muted focus:outline-none" />
        <Kbd>/</Kbd>
      </label>
      <Segmented<SourceFilter> label="Source" value={filters.source} onChange={(source) => setFilters({ source })}
        options={[{ value: "all", label: "All" }, { value: "linkedin", label: "LinkedIn" }, { value: "naukri", label: "Naukri" }]} />
      <Segmented<"any" | "above"> label="Score" value={filters.aboveThreshold ? "above" : "any"} onChange={(v) => setFilters({ aboveThreshold: v === "above" })}
        options={[{ value: "any", label: "Any score" }, { value: "above", label: `≥ ${threshold}` }]} />
      <label className="flex items-center gap-1.5 text-[12.5px] text-muted">
        <input type="checkbox" checked={filters.showExamples} onChange={(e) => setFilters({ showExamples: e.target.checked })} className="accent-[var(--accent)]" /> Examples
      </label>
      {filters.stage && <Button size="sm" variant="ghost" onClick={() => setFilters({ stage: null })}><StageDot stage={filters.stage as Stage} />{STAGE_LABEL[filters.stage]} ✕</Button>}
    </div>
  );
}
