import { Fragment, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type AgentName, type Run } from "../api";
import { useRuns, useSettings } from "../data";
import { useUI } from "../state";
import { Button, Empty, ExampleTag, Panel, cx, duration, when } from "../ui";

const AGENTS: { name: AgentName; title: string; desc: string; phase?: string }[] = [
  { name: "discovery", title: "Discovery", desc: "Searches Naukri and LinkedIn, ranks cards by title, and saves new jobs as found." },
  { name: "scoring", title: "Scoring", desc: "Hard gates, then Gemini scores each job against your resume and shortlists ≥ threshold." },
  { name: "documents", title: "Documents", desc: "Tailored ATS resume + cover letter (.docx) for shortlisted jobs, fact-checked against your resume." },
  { name: "apply", title: "Apply", desc: "LinkedIn Easy Apply and Naukri for Docs-ready jobs: fills or checks, screenshots, then auto-submits only if Settings allow and every answer is yours; otherwise waits in Review." },
];

function StatusChip({ status }: { status: Run["status"] }) {
  return <span className={cx("rounded-full px-2 text-[11.5px] font-medium", status === "ok" && "bg-good-soft text-good", status === "error" && "bg-bad-soft text-bad", status === "running" && "bg-accent-soft text-accent")}>{status}</span>;
}

/** Follows a run's log over Server-Sent Events until it ends. */
function LiveLog({ runId, onEnd }: { runId: number; onEnd: () => void }) {
  const [lines, setLines] = useState<string[]>([]);
  const [end, setEnd] = useState<{ status: string; error: string } | null>(null);
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => {
    setLines([]); setEnd(null);
    const source = new EventSource(`/api/runs/${runId}/stream`);
    source.addEventListener("log", (e) => setLines((l) => [...l, JSON.parse((e as MessageEvent).data)]));
    source.addEventListener("end", (e) => { setEnd(JSON.parse((e as MessageEvent).data)); source.close(); onEnd(); });
    source.onerror = () => source.close();
    return () => source.close();
  }, [runId, onEnd]);
  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight }); }, [lines]);
  return (
    <Panel title={<span className="flex items-center gap-2">Run #{runId} live log {end ? <StatusChip status={end.status as Run["status"]} /> : <StatusChip status="running" />}</span>}>
      <pre ref={box} aria-live="polite" className="m-0 max-h-72 overflow-y-auto whitespace-pre-wrap bg-bg px-3.5 py-2.5 font-mono text-[12px] leading-relaxed">
        {lines.join("\n") || "Waiting for output…"}
        {end?.error && <span className="text-bad">{"\n"}{end.error}</span>}
      </pre>
    </Panel>
  );
}

export function Runs() {
  const { data: runs, isLoading } = useRuns();
  const { data: settings } = useSettings();
  const { toast } = useUI();
  const qc = useQueryClient();
  const [live, setLive] = useState<number | null>(null);
  const [limit, setLimit] = useState(60);
  const [docsLimit, setDocsLimit] = useState(5);
  const [reviewOnly, setReviewOnly] = useState(true);
  const [open, setOpen] = useState<number | null>(null);
  const [log, setLog] = useState<string>("");

  const start = useMutation({
    mutationFn: ({ agent, options }: { agent: AgentName; options: Record<string, unknown> }) => api.startRun(agent, options),
    onSuccess: (run) => { setLive(run.id); toast(`${run.agent} run #${run.id} started`); qc.invalidateQueries({ queryKey: ["runs"] }); },
    onError: (e: Error) => toast(e.message),
  });
  const refresh = useRef(() => { qc.invalidateQueries(); }).current;
  const showLog = async (id: number) => {
    if (open === id) { setOpen(null); return; }
    setOpen(id); setLog("Loading…");
    setLog((await api.run(id)).log || "No log captured.");
  };

  const last = (agent: AgentName) => runs?.find((r) => r.agent === agent && !r.is_example);
  const running = (agent: AgentName) => runs?.some((r) => r.agent === agent && r.status === "running" && !r.is_example);

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {AGENTS.map((a) => {
          const prev = last(a.name);
          return (
            <section key={a.name} className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-3.5">
              <header className="flex items-center gap-2"><h3 className="mr-auto font-display text-[15px] font-semibold">{a.title}</h3>{prev ? <StatusChip status={prev.status} /> : <span className="text-[11.5px] text-muted">no runs</span>}</header>
              <p className="m-0 text-[12.5px] text-muted">{a.desc}</p>
              <div className="num text-[11.5px] text-muted">{prev ? <>last {when(prev.started_at)} · {prev.processed} processed · {prev.found} {a.name === "scoring" ? "shortlisted" : a.name === "documents" ? "with docs" : a.name === "apply" ? "submitted" : "new"}</> : "never run"}</div>
              {a.name === "documents" && <label className="flex items-center gap-1.5 text-[12px]">up to <input type="number" min={1} max={50} value={docsLimit} onChange={(e) => setDocsLimit(Number(e.target.value))} className="num w-16 rounded-md border border-line-2 bg-bg px-2 py-0.5 text-fg focus:border-accent focus:outline-none" /> jobs</label>}
              {a.name === "apply" && <label className="flex items-center gap-1.5 text-[12px]"><input type="checkbox" checked={reviewOnly} onChange={(e) => setReviewOnly(e.target.checked)} className="accent-[var(--accent)]" />review only (never auto-submit this run)</label>}
              {a.name === "scoring" && <label className="flex items-center gap-1.5 text-[12px]">up to <input type="number" min={1} max={500} value={limit} onChange={(e) => setLimit(Number(e.target.value))} className="num w-16 rounded-md border border-line-2 bg-bg px-2 py-0.5 text-fg focus:border-accent focus:outline-none" /> jobs</label>}
              <div className="mt-auto pt-1">
                {a.phase
                  ? <Button disabled title={`Arrives in ${a.phase}`}>Run now · {a.phase}</Button>
                  : <Button variant="primary" disabled={start.isPending || running(a.name)}
                      onClick={() => start.mutate({ agent: a.name, options: a.name === "discovery" ? {} : a.name === "apply" ? { auto_submit: !reviewOnly && (settings?.auto_submit ?? false) } : { limit: a.name === "documents" ? docsLimit : limit } })}>
                      {running(a.name) ? "Running…" : "Run now"}
                    </Button>}
              </div>
            </section>
          );
        })}
      </div>
      {a11yNote}
      {live != null && <LiveLog runId={live} onEnd={refresh} />}
      {isLoading ? <Empty title="Loading runs…" /> : (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full min-w-[760px] border-collapse text-[12.5px]">
            <thead><tr className="border-b border-line bg-sunk/70 text-left text-[11px] uppercase tracking-[.06em] text-muted">
              {["#", "Agent", "Started (IST)", "Duration", "Found", "Processed", "Status", ""].map((h) => <th key={h} className="px-3 py-2 font-semibold">{h}</th>)}
            </tr></thead>
            <tbody>
              {runs?.map((r) => (
                <Fragment key={r.id}>
                  <tr className="border-b border-line">
                    <td className="num px-3 py-1.5 text-muted">{r.id}</td>
                    <td className="px-3 py-1.5 font-semibold">{r.agent} {r.is_example && <ExampleTag />}</td>
                    <td className="num px-3 py-1.5">{when(r.started_at)}</td>
                    <td className="num px-3 py-1.5">{r.status === "running" ? "…" : duration(r.started_at, r.finished_at)}</td>
                    <td className="num px-3 py-1.5">{r.found}</td><td className="num px-3 py-1.5">{r.processed}</td>
                    <td className="px-3 py-1.5"><StatusChip status={r.status} /> {r.error && <span className="ml-1 text-bad" title={r.error}>{r.error.slice(0, 70)}{r.error.length > 70 ? "…" : ""}</span>}</td>
                    <td className="px-3 py-1.5 text-right">
                      {r.status === "running" && <Button size="sm" variant="ghost" onClick={() => setLive(r.id)}>Follow</Button>}
                      <Button size="sm" variant="ghost" onClick={() => showLog(r.id)}>{open === r.id ? "Hide log" : "Log"}</Button>
                    </td>
                  </tr>
                  {open === r.id && <tr className="border-b border-line"><td colSpan={8} className="bg-bg p-0"><pre className="m-0 max-h-64 overflow-y-auto whitespace-pre-wrap px-3.5 py-2 font-mono text-[11.5px]">{log}</pre></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const a11yNote = <p className="m-0 text-[12px] text-muted">Search opens a visible browser window on this PC for a few minutes. Runs started by the Windows scheduler show up here too.</p>;
