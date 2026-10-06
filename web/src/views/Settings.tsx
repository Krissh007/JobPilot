import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useSettings, useSystem } from "../data";
import { useUI } from "../state";
import { Button, Empty, Panel, cx, inputClass, labelClass } from "../ui";

export function Settings() {
  const { data: settings } = useSettings();
  const { data: system } = useSystem();
  const { toast } = useUI();
  const qc = useQueryClient();
  const [form, setForm] = useState({ threshold: 60, daily_apply_cap: 10, queries: "" });
  useEffect(() => {
    if (settings) setForm({ threshold: settings.threshold, daily_apply_cap: settings.daily_apply_cap, queries: settings.search_queries.join("\n") });
  }, [settings]);
  const save = useMutation({
    mutationFn: () => api.saveSettings({ threshold: form.threshold, daily_apply_cap: form.daily_apply_cap, search_queries: form.queries.split("\n") }),
    onSuccess: () => { toast("Settings saved"); qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["meta"] }); },
    onError: (e: Error) => toast(`Not saved: ${e.message}`),
  });
  if (!settings) return <Empty title="Loading settings…" />;
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };
  const s = settings.schedule;

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Panel title="Scoring and applying">
        <form onSubmit={submit} className="flex flex-col gap-3 p-3.5">
          <div className="grid grid-cols-2 gap-3">
            <div><label htmlFor="thr" className={labelClass}>Shortlist threshold (30–95)</label><input id="thr" type="number" min={30} max={95} value={form.threshold} onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} className={inputClass} /></div>
            <div><label htmlFor="cap" className={labelClass}>Daily apply cap (1–40)</label><input id="cap" type="number" min={1} max={40} value={form.daily_apply_cap} onChange={(e) => setForm({ ...form, daily_apply_cap: Number(e.target.value) })} className={inputClass} /></div>
          </div>
          <div><label htmlFor="qs" className={labelClass}>Search queries, one per line</label><textarea id="qs" rows={5} value={form.queries} onChange={(e) => setForm({ ...form, queries: e.target.value })} className={inputClass} /></div>
          <p className="m-0 text-[12px] text-muted">The search agent searches each query on LinkedIn and Naukri (Hyderabad). More queries mean longer runs.</p>
          <div><Button variant="primary" type="submit" disabled={save.isPending}>Save settings</Button></div>
        </form>
      </Panel>
      <div className="flex flex-col gap-3">
        <Panel title="Schedule">
          <div className="p-3.5 text-[12.5px]">
            <p className="m-0">Search + score every <b>{s.every_hours} h</b> between <b className="num">{s.start}</b> and <b className="num">{s.end}</b> ({s.timezone}).</p>
            <p className="mb-0 mt-1.5 text-muted">Registered with Windows Task Scheduler in Phase 8. Not active yet.</p>
          </div>
        </Panel>
        <Panel title="Gemini and paths">
          <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 p-3.5 text-[12.5px]">
            <dt className="text-muted">API key</dt><dd className={cx("m-0", system?.gemini_key_set ? "text-good" : "text-bad")}>{system ? (system.gemini_key_set ? "set in .env (hidden)" : "missing: add GEMINI_API_KEY to .env") : "…"}</dd>
            <dt className="text-muted">Model</dt><dd className="m-0 font-mono">{system?.gemini_model || "—"}{system?.gemini_fallback_models ? ` → ${system.gemini_fallback_models}` : ""} <span className="text-muted">· {system?.gemini_rpm}/min</span></dd>
            <dt className="text-muted">Resume</dt><dd className="m-0 break-all font-mono text-[12px]">{system?.resume_path}</dd>
            <dt className="text-muted">Legacy agent</dt><dd className="m-0 break-all font-mono text-[12px]">{system?.legacy_agent_dir}</dd>
          </dl>
        </Panel>
        <div role="note" className="rounded-lg border border-warn/40 bg-warn-soft p-3.5 text-[12.5px]">
          <b className="text-warn">Site terms.</b> Automating a signed-in LinkedIn or Naukri account is against both sites' user agreements and can get the account restricted. JobPilot keeps a low daily cap, waits 20–90 s between jobs, uses one tab, never solves CAPTCHAs or types passwords, and never submits without your approval. The risk is reduced, not removed.
        </div>
      </div>
    </div>
  );
}
