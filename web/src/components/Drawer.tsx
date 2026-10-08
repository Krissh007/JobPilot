import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, STAGES, STAGE_LABEL, type JobDetail, type Stage } from "../api";
import { useJob, useMoveJob } from "../data";
import { useUI } from "../state";
import { Button, ExampleTag, StageDot, cx, expRange, inputClass, labelClass, when } from "../ui";

export function Drawer({ threshold }: { threshold: number }) {
  const { drawer, openJob } = useUI();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea, select") != null;
      if (e.key === "Escape") openJob(null);
      if ((e.key === "n" || e.key === "N") && !typing && !e.ctrlKey && !e.metaKey) { e.preventDefault(); openJob("new"); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openJob]);
  if (drawer == null) return null;
  return (
    <>
      <div className="fixed inset-0 z-30 bg-[#0a141a]/30" onClick={() => openJob(null)} aria-hidden />
      <aside role="dialog" aria-modal="true" aria-label={drawer === "new" ? "Add job" : "Job details"}
        className="drawer-in fixed inset-y-0 right-0 z-40 flex w-full max-w-[540px] flex-col overflow-y-auto border-l border-line bg-surface shadow-2xl">
        {drawer === "new" ? <AddJob /> : <JobPanel id={drawer} threshold={threshold} />}
      </aside>
    </>
  );
}

function Bar({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="grid grid-cols-[78px_1fr_28px] items-center gap-2 text-[12px] text-muted">
      <span>{label}</span>
      <span className="h-[7px] overflow-hidden rounded-full bg-sunk"><b className="block h-full rounded-full bg-accent" style={{ width: `${value ?? 0}%` }} /></span>
      <span className="num text-right text-fg">{value == null ? "—" : Math.round(value)}</span>
    </div>
  );
}

function JobPanel({ id, threshold }: { id: number; threshold: number }) {
  const { openJob, toast } = useUI();
  const { data: job, isLoading, isError } = useJob(id);
  if (isLoading) return <div className="p-6 text-muted">Loading…</div>;
  if (isError || !job) return <div className="p-6 text-muted">This job no longer exists. <Button size="sm" onClick={() => openJob(null)}>Close</Button></div>;
  return <JobBody job={job} threshold={threshold} onClose={() => openJob(null)} toast={toast} />;
}

function JobBody({ job, threshold, onClose, toast }: { job: JobDetail; threshold: number; onClose: () => void; toast: (m: string) => void }) {
  const qc = useQueryClient();
  const move = useMoveJob();
  const [notes, setNotes] = useState(job.notes);
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => setNotes(job.notes), [job.id, job.notes]);

  const saveNotes = useMutation({
    mutationFn: (value: string) => api.patchJob(job.id, { notes: value }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["job", job.id] }); toast("Notes saved"); },
    onError: (e: Error) => toast(e.message),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteJob(job.id),
    onSuccess: () => { toast(`Deleted ${job.company}: ${job.title}`); qc.invalidateQueries(); onClose(); },
  });
  const queue = useMutation({
    mutationFn: () => api.startRun("apply", { job_id: job.id }),
    onSuccess: (run) => toast(`Apply run #${run.id} started for ${job.company}. Chrome opens; follow it in Runs / Review.`),
    onError: (e: Error) => toast(e.message),
  });
  const external = job.applications.some((a) => a.reason === "not_easy_apply" || a.reason === "external");
  const portal = job.source === "naukri" ? "Naukri" : "LinkedIn";
  const canQueue = (job.source === "linkedin" || job.source === "naukri") && (job.status === "docs_ready" || job.status === "shortlisted") && !job.is_example && !external;
  const folder = useMutation({ mutationFn: () => api.openFolder(job.id), onSuccess: (r) => toast(`Opened ${r.opened}`), onError: (e: Error) => toast(e.message) });

  const scoreColor = job.score == null ? "text-muted" : job.score >= threshold ? "text-good" : job.score >= threshold - 15 ? "text-warn" : "text-muted";

  return (
    <div className="flex flex-col">
      <header className="flex items-start gap-3 border-b border-line px-5 py-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-1.5 text-[12px] text-muted">
            <span>{job.company}</span>·<span className="capitalize">{job.source}</span>{job.location && <>·<span>{job.location}</span></>}{job.is_example && <ExampleTag />}
          </div>
          <h2 className="font-display text-[20px] font-bold leading-tight">{job.title}</h2>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">Esc</Button>
      </header>

      <div className="flex flex-col gap-4 px-5 py-4">
        <div className="grid grid-cols-[auto_1fr] items-center gap-5">
          <div>
            <div className={cx("num text-[46px] font-semibold leading-none tracking-tighter", scoreColor)}>{job.score == null ? "—" : Math.round(job.score)}</div>
            <div className="mt-1 text-[11.5px] text-muted">fit · rule <span className="num">{job.legacy_score == null ? "—" : Math.round(job.legacy_score)}</span></div>
          </div>
          <div className="flex flex-col gap-2">
            <Bar label="Skills" value={job.score_skills} /><Bar label="Experience" value={job.score_experience} /><Bar label="Domain" value={job.score_domain} />
          </div>
        </div>
        {job.score_reason
          ? <p className="m-0 rounded-r-md border-l-[3px] border-accent bg-accent-soft px-3 py-2 text-[13px]">{job.score_reason}</p>
          : <p className="m-0 text-[12.5px] text-muted">Not scored yet. The score agent will pick it up on its next run.</p>}
        {job.skills.length > 0 && <div className="flex flex-wrap gap-1">{job.skills.map((s) => <span key={s} className="rounded bg-sunk px-2 py-0.5 text-[11.5px]">{s}</span>)}</div>}

        <div className="grid grid-cols-2 overflow-hidden rounded-md border border-line text-[13px]">
          <div className="border-b border-r border-line px-3 py-2">
            <label htmlFor="status" className="block text-[11px] text-muted">Status</label>
            <select id="status" value={job.status} className="-ml-1 w-full bg-transparent font-medium focus:outline-none"
              onChange={(e) => move.mutate({ id: job.id, status: e.target.value as Stage }, { onSuccess: () => toast(`Moved to ${STAGE_LABEL[e.target.value as Stage]}`) })}>
              {STAGES.map((s) => <option key={s} value={s}>{STAGE_LABEL[s]}</option>)}
            </select>
          </div>
          <div className="border-b border-line px-3 py-2"><span className="block text-[11px] text-muted">Experience</span>{expRange(job.exp_min, job.exp_max)}</div>
          <div className="border-r border-line px-3 py-2"><span className="block text-[11px] text-muted">Found</span>{when(job.found_at)}</div>
          <div className="px-3 py-2"><span className="block text-[11px] text-muted">Applied</span>{when(job.applied_at)}</div>
        </div>

        {external && (
          <p className="m-0 rounded-md border border-line-2 bg-sunk px-3 py-2 text-[12.5px]"><b>External apply.</b> {portal} sends this job to the company's site, so the agent can't apply. Use <i>Open posting</i> with the tailored documents below.</p>
        )}
        <div className="flex flex-wrap gap-1.5 border-t border-line pt-3">
          <Button variant="primary" disabled={!canQueue || queue.isPending} onClick={() => queue.mutate()}
            title={canQueue ? (job.source === "naukri" ? "Check the Naukri posting and wait for your Approve (on Naukri, Apply is the submit). Auto-submit only if allowed in Settings." : "Fill the LinkedIn Easy Apply form now. Auto-submit only if allowed in Settings.") : "Only LinkedIn and Naukri jobs that are Shortlisted or Docs ready"}>Queue for apply</Button>
          <a href={job.url} target="_blank" rel="noreferrer noopener"><Button>Open posting ↗</Button></a>
          <a href={job.docs.resume ? `/api/jobs/${job.id}/docs/resume` : undefined} aria-disabled={!job.docs.resume}><Button disabled={!job.docs.resume} title={job.docs.resume ? "" : "Not generated yet. Run the docs agent from Runs"}>Resume.docx</Button></a>
          <a href={job.docs.cover ? `/api/jobs/${job.id}/docs/cover` : undefined} aria-disabled={!job.docs.cover}><Button disabled={!job.docs.cover}>Cover.docx</Button></a>
          <a href={job.docs.report ? `/api/jobs/${job.id}/docs/report` : undefined} aria-disabled={!job.docs.report}><Button disabled={!job.docs.report} title="JD keywords covered/missing and fact-check fixes">Report</Button></a>
          <Button disabled={!job.docs.folder} onClick={() => folder.mutate()}>Open folder</Button>
        </div>

        <div>
          <label htmlFor="notes" className={labelClass}>Notes</label>
          <textarea id="notes" value={notes} onChange={(e) => setNotes(e.target.value)} rows={3}
            onBlur={() => notes !== job.notes && saveNotes.mutate(notes)} placeholder="Recruiter name, referral, follow-up date…" className={inputClass} />
        </div>

        <details className="group">
          <summary className="cursor-pointer select-none text-[12.5px] font-semibold">Job description <span className="font-normal text-muted">({job.jd_text.length.toLocaleString()} chars)</span></summary>
          <div className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-bg p-3 text-[12.5px] leading-relaxed">{job.jd_text || "No description captured."}</div>
        </details>

        <div>
          <h3 className={labelClass}>History</h3>
          <ol className="flex flex-col gap-1 text-[12.5px]">
            {job.history.map((h, i) => (
              <li key={i} className="flex items-center gap-2">
                <span className="num w-[108px] shrink-0 text-muted">{when(h.changed_at)}</span>
                {h.from_status ? <><StageDot stage={h.from_status} />{STAGE_LABEL[h.from_status]} →</> : "created as"}
                <StageDot stage={h.to_status} />{STAGE_LABEL[h.to_status]}
                <span className="ml-auto rounded bg-sunk px-1.5 text-[11px] text-muted">{h.actor}</span>
              </li>
            ))}
          </ol>
        </div>

        <div className="border-t border-line pt-3">
          {confirmDelete
            ? <div className="flex items-center gap-2 text-[12.5px]">Delete this job and its history? <Button variant="danger" size="sm" onClick={() => remove.mutate()}>Delete</Button><Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>Keep</Button></div>
            : <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(true)}>Delete job</Button>}
        </div>
      </div>
    </div>
  );
}

function AddJob() {
  const { openJob, toast } = useUI();
  const qc = useQueryClient();
  const [form, setForm] = useState({ url: "", title: "", company: "", location: "Hyderabad", source: "manual", jd_text: "" });
  const create = useMutation({
    mutationFn: () => api.createJob(form),
    onSuccess: (job) => { toast(`Added ${job.company}: ${job.title}. The score agent will score it.`); qc.invalidateQueries(); openJob(job.id); },
    onError: (e: Error) => {
      const id = e instanceof ApiError && e.status === 409 ? (e.detail as { job_id?: number }).job_id : undefined;
      toast(id ? "Already in the pipeline, opening it" : e.message);
      if (id) openJob(id);
    },
  });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate(); };

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 px-5 py-4">
      <header className="flex items-center"><h2 className="mr-auto font-display text-[20px] font-bold">Add a job</h2><Button type="button" variant="ghost" size="sm" onClick={() => openJob(null)}>Esc</Button></header>
      <div><label htmlFor="url" className={labelClass}>Job URL</label><input id="url" required type="url" value={form.url} onChange={set("url")} className={inputClass} placeholder="https://www.linkedin.com/jobs/view/…" /></div>
      <div className="grid grid-cols-2 gap-3">
        <div><label htmlFor="title" className={labelClass}>Title</label><input id="title" required value={form.title} onChange={set("title")} className={inputClass} /></div>
        <div><label htmlFor="company" className={labelClass}>Company</label><input id="company" required value={form.company} onChange={set("company")} className={inputClass} /></div>
        <div><label htmlFor="location" className={labelClass}>Location</label><input id="location" value={form.location} onChange={set("location")} className={inputClass} /></div>
        <div><label htmlFor="source" className={labelClass}>Source</label>
          <select id="source" value={form.source} onChange={set("source")} className={inputClass}><option value="manual">manual</option><option value="linkedin">linkedin</option><option value="naukri">naukri</option></select></div>
      </div>
      <div><label htmlFor="jd" className={labelClass}>Job description</label><textarea id="jd" rows={10} value={form.jd_text} onChange={set("jd_text")} className={inputClass} placeholder="Paste the JD so it can be scored" /></div>
      <div><Button variant="primary" type="submit" disabled={create.isPending}>{create.isPending ? "Adding…" : "Add job"}</Button></div>
    </form>
  );
}
