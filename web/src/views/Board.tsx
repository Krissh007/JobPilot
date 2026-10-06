import { useState } from "react";
import { STAGES, STAGE_LABEL, type JobQuery, type JobSummary, type Stage } from "../api";
import { useJobs, useMoveJob } from "../data";
import { useUI } from "../state";
import { Empty, ExampleTag, ScoreNum, SourceTag, StageDot, ago, cx } from "../ui";

export function useJobQuery(threshold: number, extra: Partial<JobQuery> = {}): JobQuery {
  const { filters } = useUI();
  return {
    q: filters.q,
    source: filters.source === "all" ? undefined : filters.source,
    min_score: filters.aboveThreshold ? threshold : undefined,
    include_examples: filters.showExamples,
    ...extra,
  };
}

export function JobRow({ job, threshold, selected, onOpen, draggable }:
  { job: JobSummary; threshold: number; selected: boolean; onOpen: () => void; draggable?: boolean }) {
  return (
    <div role="button" tabIndex={0} draggable={draggable} onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onOpen())}
      onDragStart={(e) => { e.dataTransfer.setData("text/job", String(job.id)); e.dataTransfer.effectAllowed = "move"; }}
      className={cx("grid cursor-pointer grid-cols-[30px_1fr] gap-2 border-b border-line px-2.5 py-2 hover:bg-sunk/70",
        selected && "bg-accent-soft shadow-[inset_2px_0_0_var(--accent)] hover:bg-accent-soft")}>
      <ScoreNum score={job.score} threshold={threshold} className="pt-px text-right text-[13px]" />
      <div className="min-w-0">
        <div className="truncate font-semibold" title={job.title}>{job.title}</div>
        <div className="flex min-w-0 items-center gap-1.5 text-[12px] text-muted">
          <span className="truncate" title={job.company}>{job.company}</span>
          <SourceTag source={job.source} /><span className="shrink-0">{ago(job.found_at)}</span>
          {job.is_example && <ExampleTag />}
        </div>
      </div>
    </div>
  );
}

export function Board({ threshold }: { threshold: number }) {
  const { filters, drawer, openJob, toast } = useUI();
  const query = useJobQuery(threshold);
  const { data, isLoading } = useJobs(query);
  const move = useMoveJob();
  const [over, setOver] = useState<Stage | null>(null);
  const stages = filters.stage ? [filters.stage] : STAGES;

  if (isLoading) return <Empty title="Loading your pipeline…" />;
  if (!data?.total && !filters.q && filters.source === "all") return <Empty title="No jobs yet">Run a search from the header, or add one with <b>Add job</b>.</Empty>;

  const drop = (stage: Stage, id: number) => {
    const job = data?.items.find((j) => j.id === id);
    if (!job || job.status === stage) return;
    move.mutate({ id, status: stage }, {
      onSuccess: () => toast(`${job.company}: ${STAGE_LABEL[job.status]} → ${STAGE_LABEL[stage]}`),
      onError: (e) => toast(`Could not move: ${(e as Error).message}`),
    });
  };

  return (
    <>
      <div className="grid auto-cols-[minmax(188px,1fr)] grid-flow-col gap-2 overflow-x-auto pb-2">
        {stages.map((stage) => {
          const jobs = data?.items.filter((j) => j.status === stage) ?? [];
          return (
            <section key={stage} aria-label={STAGE_LABEL[stage]}
              onDragOver={(e) => { e.preventDefault(); setOver(stage); }} onDragLeave={() => setOver(null)}
              onDrop={(e) => { e.preventDefault(); setOver(null); drop(stage, Number(e.dataTransfer.getData("text/job"))); }}
              className={cx("flex max-h-[calc(100vh-280px)] min-h-[420px] flex-col overflow-hidden rounded-lg border bg-surface",
                over === stage ? "border-accent outline outline-2 outline-dashed outline-accent/50" : "border-line")}>
              <header className="flex items-center gap-1.5 border-b border-line bg-sunk/70 px-2.5 py-2 text-[12px] font-semibold">
                <StageDot stage={stage} />{STAGE_LABEL[stage]}<span className="num ml-auto font-medium text-muted">{jobs.length}</span>
              </header>
              <div className="flex-1 overflow-y-auto">
                {jobs.map((job) => <JobRow key={job.id} job={job} threshold={threshold} draggable selected={drawer === job.id} onOpen={() => openJob(job.id)} />)}
                {jobs.length === 0 && <div className="px-2.5 py-3 text-[12px] text-muted">Drop a job here</div>}
              </div>
            </section>
          );
        })}
      </div>
      <p className="text-[12px] text-muted">Drag rows between columns to change status. Every move is recorded in the job's history.</p>
    </>
  );
}
