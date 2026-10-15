import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useMeta, useTableCounts } from "../data";
import { useUI } from "../state";
import { Button, Panel } from "../ui";

const SCHEMA: [string, string[]][] = [
  ["jobs", ["id PK", "url UNIQUE", "url_canonical · dedupe_hash", "title · company · source · location", "exp_min · exp_max", "jd_text (FTS5)", "skills JSON", "score + skills/experience/domain", "score_reason · legacy_score", "status ENUM", "resume/cover paths · resume_version", "found_at · applied_at · notes"]],
  ["status_history", ["job_id FK", "from_status → to_status", "changed_at", "actor user|agent"]],
  ["agent_runs", ["agent ENUM", "started_at · finished_at", "found · processed", "status · error · log"]],
  ["applications", ["job_id FK", "state ENUM", "screenshot_path", "created_at · decided_at"]],
  ["application_fields", ["application_id FK", "step · field_name", "value_used", "source · needs_review"]],
  ["answers", ["question · answer", "tags · times_used", "created_at · updated_at"]],
  ["settings", ["key PK", "value JSON"]],
];

export function Database() {
  const { data: counts } = useTableCounts();
  const { data: meta } = useMeta();
  const { toast } = useUI();
  const qc = useQueryClient();
  const [confirmPurge, setConfirmPurge] = useState(false);
  const purge = useMutation({ mutationFn: api.purgeExamples, onSuccess: (r) => { toast(`Removed ${r.jobs} example jobs and their rows`); setConfirmPurge(false); qc.invalidateQueries(); } });
  const seed = useMutation({ mutationFn: api.seedExamples, onSuccess: (r) => { toast(r.added_jobs ? `Added ${r.added_jobs} example jobs` : "Examples are already there"); qc.invalidateQueries(); } });

  return (
    <div className="flex flex-col gap-3">
      <p className="m-0 text-[12.5px] text-muted">One database: <code className="rounded bg-sunk px-1.5 font-mono text-[12px]">E:\JobPilot\data\jobpilot.db</code> (SQLite, WAL, Alembic-managed).</p>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {SCHEMA.map(([table, cols]) => (
          <Panel key={table} title={<span className="font-mono">{table}</span>} actions={<span className="num text-[12px] text-muted">{counts?.[table] ?? "…"} rows</span>}>
            <ul className="m-0 flex list-none flex-col gap-0.5 px-3.5 py-2.5 font-mono text-[11.5px] text-muted">{cols.map((c) => <li key={c}>{c}</li>)}</ul>
          </Panel>
        ))}
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Export">
          <div className="flex flex-col gap-2 p-3.5 text-[12.5px] text-muted">
            <p className="m-0">Download the pipeline for Excel or a backup.</p>
            <div className="flex gap-1.5"><a href="/api/export/jobs.csv"><Button>Jobs as CSV</Button></a><a href="/api/export/all.json"><Button>Everything as JSON</Button></a></div>
          </div>
        </Panel>
        <Panel title="Example rows">
          <div className="flex flex-col gap-2 p-3.5 text-[12.5px] text-muted">
            <p className="m-0">{meta?.example_jobs ? `${meta.example_jobs} fictional example jobs (plus runs, answers and applications) show how each screen looks.` : "No example rows."}</p>
            <div className="flex gap-1.5">
              {meta?.example_jobs
                ? confirmPurge
                  ? <><Button variant="danger" onClick={() => purge.mutate()}>Delete all examples</Button><Button variant="ghost" onClick={() => setConfirmPurge(false)}>Keep</Button></>
                  : <Button onClick={() => setConfirmPurge(true)}>Remove examples</Button>
                : <Button onClick={() => seed.mutate()}>Add examples</Button>}
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}
