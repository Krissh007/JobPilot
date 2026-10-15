import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useSettings, useSystem } from "../data";
import { FiltersPanel } from "../components/FiltersPanel";
import { SchedulePanel } from "../components/SchedulePanel";
import { useUI } from "../state";
import { Button, Empty, Panel, cx, inputClass, labelClass } from "../ui";

export function Settings() {
  const { data: settings } = useSettings();
  const { data: system } = useSystem();
  const { toast } = useUI();
  const qc = useQueryClient();
  const [form, setForm] = useState({ threshold: 60, daily_apply_cap: 10, queries: "", auto_submit: false, review_wait_minutes: 20, day: "mon", time: "09:00", easy_apply_only: true, park_incomplete: true });
  useEffect(() => {
    if (settings) setForm({ threshold: settings.threshold, daily_apply_cap: settings.daily_apply_cap, queries: settings.search_queries.join("\n"),
      auto_submit: settings.auto_submit, review_wait_minutes: settings.review_wait_minutes, day: settings.weekly_review.day, time: settings.weekly_review.time,
      easy_apply_only: settings.linkedin_easy_apply_only, park_incomplete: settings.park_incomplete });
  }, [settings]);
  const save = useMutation({
    mutationFn: () => api.saveSettings({ threshold: form.threshold, daily_apply_cap: form.daily_apply_cap, search_queries: form.queries.split("\n"),
      auto_submit: form.auto_submit, review_wait_minutes: form.review_wait_minutes, weekly_review: { day: form.day, time: form.time },
      linkedin_easy_apply_only: form.easy_apply_only, park_incomplete: form.park_incomplete }),
    onSuccess: () => { toast("Settings saved"); qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["meta"] }); qc.invalidateQueries({ queryKey: ["schedule"] }); },
    onError: (e: Error) => toast(`Not saved: ${e.message}`),
  });
  if (!settings) return <Empty title="Loading settings…" />;
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };

  return (
    <div className="grid gap-3 lg:grid-cols-2 [&>*]:min-w-0">
      <Panel title="Scoring and applying">
        <form onSubmit={submit} className="flex flex-col gap-3 p-3.5">
          <div className="grid grid-cols-2 gap-3">
            <div><label htmlFor="thr" className={labelClass}>Shortlist threshold (30–95)</label><input id="thr" type="number" min={30} max={95} value={form.threshold} onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} className={inputClass} /></div>
            <div><label htmlFor="cap" className={labelClass}>Daily apply cap (1–40)</label><input id="cap" type="number" min={1} max={40} value={form.daily_apply_cap} onChange={(e) => setForm({ ...form, daily_apply_cap: Number(e.target.value) })} className={inputClass} /></div>
          </div>
          <div><label htmlFor="qs" className={labelClass}>Search queries, one per line</label><textarea id="qs" rows={5} value={form.queries} onChange={(e) => setForm({ ...form, queries: e.target.value })} className={inputClass} /></div>
          <p className="m-0 text-[12px] text-muted">The search agent searches each query on LinkedIn and Naukri (Hyderabad). More queries mean longer runs.</p>
          <label className="flex items-start gap-2 text-[13px]">
            <input type="checkbox" checked={form.easy_apply_only} onChange={(e) => setForm({ ...form, easy_apply_only: e.target.checked })} className="mt-0.5 accent-[var(--accent)]" />
            <span><b>LinkedIn: Easy Apply jobs only</b><br /><span className="text-muted">Uses LinkedIn's own Easy Apply filter. Jobs that send you to a company site can't be applied to by the agent.</span></span>
          </label>
          <fieldset className={cx("m-0 rounded-md border p-3", form.auto_submit ? "border-warn/50 bg-warn-soft/40" : "border-line")}>
            <legend className={labelClass}>Apply agent</legend>
            <label className="flex items-start gap-2 text-[13px]">
              <input type="checkbox" checked={form.auto_submit} onChange={(e) => setForm({ ...form, auto_submit: e.target.checked })} className="mt-0.5 accent-[var(--accent)]" />
              <span><b>Auto-submit fully answered applications</b><br /><span className="text-muted">Only Docs-ready LinkedIn and Naukri jobs ≥ threshold where every field came from your resume/profile or answer bank. Anything with a Gemini-drafted, empty or consent answer waits in Review. Naukri chat questions are only ever answered from the answer bank. Off = every application waits for your Approve.</span></span>
            </label>
            <label className="mt-2 flex items-start gap-2 text-[13px]">
              <input type="checkbox" checked={form.park_incomplete} onChange={(e) => setForm({ ...form, park_incomplete: e.target.checked })} className="mt-0.5 accent-[var(--accent)]" />
              <span><b>Save stuck applications for me to finish</b><br /><span className="text-muted">When a form needs an answer your answer bank doesn't have, the agent fills everything else, saves LinkedIn's draft, lists the blanks in Review and moves on to the next job. Off = it keeps the browser window open and waits for you.</span></span>
            </label>
            <div className="mt-2 grid grid-cols-3 gap-3">
              <div><label htmlFor="wait" className={labelClass}>Review wait (min)</label><input id="wait" type="number" min={5} max={120} value={form.review_wait_minutes} onChange={(e) => setForm({ ...form, review_wait_minutes: Number(e.target.value) })} className={inputClass} /></div>
              <div><label htmlFor="wday" className={labelClass}>Weekly review day</label>
                <select id="wday" value={form.day} onChange={(e) => setForm({ ...form, day: e.target.value })} className={inputClass}>{["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((d) => <option key={d} value={d}>{d}</option>)}</select></div>
              <div><label htmlFor="wtime" className={labelClass}>Time (IST)</label><input id="wtime" type="time" value={form.time} onChange={(e) => setForm({ ...form, time: e.target.value })} className={inputClass} /></div>
            </div>
          </fieldset>
          <div><Button variant="primary" type="submit" disabled={save.isPending}>Save settings</Button></div>
        </form>
      </Panel>
      <div className="flex flex-col gap-3">
        <SchedulePanel settings={settings} />
        <FiltersPanel settings={settings} />
        <Panel title="Gemini and paths">
          <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 p-3.5 text-[12.5px]">
            <dt className="text-muted">API key</dt><dd className={cx("m-0", system?.gemini_key_set ? "text-good" : "text-bad")}>{system ? (system.gemini_key_set ? "set in .env (hidden)" : "missing: add GEMINI_API_KEY to .env") : "…"}</dd>
            <dt className="text-muted">Model</dt><dd className="m-0 font-mono">{system?.gemini_model || "—"}{system?.gemini_fallback_models ? ` → ${system.gemini_fallback_models}` : ""} <span className="text-muted">· {system?.gemini_rpm}/min</span></dd>
            <dt className="text-muted">Resume</dt><dd className="m-0 break-all font-mono text-[12px]">{system?.resume_path}</dd>
          </dl>
        </Panel>
        <div role="note" className="rounded-lg border border-warn/40 bg-warn-soft p-3.5 text-[12.5px]">
          <b className="text-warn">Site terms.</b> Automating a signed-in LinkedIn or Naukri account is against both sites' user agreements and can get the account restricted. JobPilot keeps a low daily cap, waits 20–90 s between jobs, uses one tab, and never solves CAPTCHAs or types passwords. {settings.auto_submit ? "Auto-submit is ON: fully answered Docs-ready jobs are submitted without asking; everything else waits for your approval." : "Auto-submit is OFF: nothing is submitted without your approval."} The risk is reduced, not removed.
        </div>
      </div>
    </div>
  );
}
