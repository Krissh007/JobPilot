import { useState } from "react";
import { STAGE_LABEL } from "../api";
import { useJobs } from "../data";
import { useUI } from "../state";
import { Empty, ExampleTag, ScoreNum, Segmented, SourceTag, StageDot, ago, cx, expRange } from "../ui";
import { useJobQuery } from "./Board";

const COLUMNS: [string, string, string][] = [
  ["score", "Score", "w-20 whitespace-nowrap"], ["title", "Role", ""], ["company", "Company", ""], ["location", "Location", ""],
  ["", "Exp", "w-20"], ["source", "Src", "w-14"], ["legacy_score", "Rule", "w-14"], ["status", "Status", "w-32"], ["found_at", "Found", "w-16"],
];

export function Jobs({ threshold }: { threshold: number }) {
  const { filters, drawer, openJob } = useUI();
  const [sort, setSort] = useState<[string, "asc" | "desc"]>(["score", "desc"]);
  const [match, setMatch] = useState<"all" | "best" | "maybe" | "eliminate">("all");
  const query = useJobQuery(threshold, { status: filters.stage ? [filters.stage] : undefined, sort: sort[0], order: sort[1], match: match === "all" ? undefined : match });
  const { data, isLoading } = useJobs(query);

  if (isLoading) return <Empty title="Loading jobs…" />;
  const toggle = (key: string) => setSort(([k, o]) => [key, k === key && o === "desc" ? "asc" : "desc"]);

  return (
    <>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Segmented label="Match" value={match} onChange={setMatch} options={[
          { value: "all", label: "All" }, { value: "best", label: "Best matches" },
          { value: "maybe", label: "You might like" }, { value: "eliminate", label: "Unsuitable" }]} />
        <span className="text-[12px] text-muted">
          {match === "best" ? "Your role titles, experience 2–6 yrs, scored at or above the threshold."
            : match === "maybe" ? "Similar titles whose description, skills and experience fit. Not queued for apply; open one to decide."
            : match === "eliminate" ? "Wrong title, experience outside 2–6 yrs, or a similar title whose skills do not fit." : ""}
        </span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full min-w-[860px] border-collapse">
          <thead>
            <tr className="border-b border-line bg-sunk/70 text-left text-[11px] uppercase tracking-[.06em] text-muted">
              {COLUMNS.map(([key, label, width]) => (
                <th key={label} className={cx("px-3 py-2 font-semibold", width)} aria-sort={sort[0] === key ? (sort[1] === "asc" ? "ascending" : "descending") : undefined}>
                  {key ? <button onClick={() => toggle(key)} className="uppercase hover:text-fg">{label}{sort[0] === key ? (sort[1] === "asc" ? " ↑" : " ↓") : ""}</button> : label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data?.items.map((job) => (
              <tr key={job.id} onClick={() => openJob(job.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && openJob(job.id)}
                className={cx("cursor-pointer border-b border-line last:border-0 hover:bg-sunk/60", drawer === job.id && "bg-accent-soft")}>
                <td className="px-3 py-1.5"><ScoreNum score={job.score} threshold={threshold} /></td>
                <td className="max-w-[340px] px-3 py-1.5"><div className="truncate font-semibold" title={job.title}>{job.title} {job.is_example && <ExampleTag />}{job.match_group === "maybe" && <span className="ml-1.5 rounded bg-accent-soft px-1.5 text-[10.5px] font-medium text-accent" title={job.match_reason}>might like</span>}</div></td>
                <td className="max-w-[220px] truncate px-3 py-1.5" title={job.company}>{job.company}</td>
                <td className="max-w-[180px] truncate px-3 py-1.5 text-muted" title={job.location}>{job.location || "—"}</td>
                <td className="num px-3 py-1.5 text-muted">{expRange(job.exp_min, job.exp_max)}</td>
                <td className="px-3 py-1.5"><SourceTag source={job.source} /></td>
                <td className="num px-3 py-1.5 text-muted">{job.legacy_score == null ? "—" : Math.round(job.legacy_score)}</td>
                <td className="px-3 py-1.5"><span className="inline-flex items-center gap-1.5 whitespace-nowrap"><StageDot stage={job.status} />{STAGE_LABEL[job.status]}</span></td>
                <td className="num px-3 py-1.5 text-muted">{ago(job.found_at)}</td>
              </tr>
            ))}
            {data?.items.length === 0 && <tr><td colSpan={COLUMNS.length} className="px-3 py-6 text-center text-muted">No jobs match these filters.</td></tr>}
          </tbody>
        </table>
      </div>
      <p className="num mt-2 text-[12px] text-muted">{data?.items.length ?? 0} of {data?.total ?? 0} jobs · "Rule" is the legacy agent's rule-based score</p>
    </>
  );
}
