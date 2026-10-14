import { useEffect, useState, type ChangeEvent, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Settings } from "../api";
import { useUI } from "../state";
import { Button, Panel, inputClass, labelClass } from "../ui";

const lines = (text: string) => text.split("\n").map((s) => s.trim()).filter(Boolean);

function formFrom(settings: Settings) {
  const d = settings.discovery;
  return {
    keywords: d.keywords.join("\n"), locations: d.locations.join("\n"), sources: d.sources, use_variants: d.use_variants,
    max_posted_days: d.max_posted_days, exp_min: settings.scoring.exp_min, exp_max: settings.scoring.exp_max,
    enrich_per_query: d.enrich_per_query, backlog_budget: d.backlog_budget,
    max_cards_per_query: d.max_cards_per_query, max_pages_per_query: d.max_pages_per_query,
  };
}
type FormState = ReturnType<typeof formFrom>;

/** Search and scoring filters for the redesigned agents (Discovery, and Scoring & Shortlisting). */
export function FiltersPanel({ settings }: { settings: Settings }) {
  const { toast } = useUI();
  const qc = useQueryClient();
  const [form, setForm] = useState<FormState>(() => formFrom(settings));
  useEffect(() => setForm(formFrom(settings)), [settings]);

  const save = useMutation({
    mutationFn: () => api.saveSettings({
      discovery: {
        ...settings.discovery,
        keywords: lines(form.keywords), locations: lines(form.locations), sources: form.sources, use_variants: form.use_variants,
        max_posted_days: form.max_posted_days, enrich_per_query: form.enrich_per_query, backlog_budget: form.backlog_budget,
        max_cards_per_query: form.max_cards_per_query, max_pages_per_query: form.max_pages_per_query,
      },
      scoring: { exp_min: form.exp_min, exp_max: form.exp_max },
    }),
    onSuccess: () => { toast("Filters saved"); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e: Error) => toast(`Not saved: ${e.message}`),
  });
  const toggle = (src: string) =>
    setForm({ ...form, sources: form.sources.includes(src) ? form.sources.filter((s) => s !== src) : [...form.sources, src] });
  const num = (key: keyof FormState) => (e: ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: Number(e.target.value) });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };

  return (
    <Panel title="Search filters" actions={<span className="text-[12px] text-muted">{settings.pipeline === "redesign" ? "in use" : "used when the agents are set to Redesign"}</span>}>
      <form onSubmit={submit} className="flex flex-col gap-3 p-3.5">
        <div className="grid grid-cols-2 gap-3">
          <div><label htmlFor="f-loc" className={labelClass}>Locations, one per line</label><textarea id="f-loc" rows={3} value={form.locations} onChange={(e) => setForm({ ...form, locations: e.target.value })} className={inputClass} /></div>
          <div><label htmlFor="f-kw" className={labelClass}>Job titles, one per line</label><textarea id="f-kw" rows={3} value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} className={inputClass} /></div>
        </div>
        <label className="flex items-start gap-2 text-[13px]">
          <input type="checkbox" checked={form.use_variants} onChange={(e) => setForm({ ...form, use_variants: e.target.checked })} className="mt-0.5 accent-[var(--accent)]" />
          <span><b>Also search the registered alternative titles</b><br /><span className="text-muted">{settings.discovery.variants.join(", ")}</span></span>
        </label>
        <div className="grid grid-cols-3 gap-3">
          <div><label htmlFor="f-days" className={labelClass}>Posted within (days)</label><input id="f-days" type="number" min={1} max={60} value={form.max_posted_days} onChange={num("max_posted_days")} className={inputClass} /></div>
          <div><label htmlFor="f-emin" className={labelClass}>Experience min (yrs)</label><input id="f-emin" type="number" min={0} max={40} value={form.exp_min} onChange={num("exp_min")} className={inputClass} /></div>
          <div><label htmlFor="f-emax" className={labelClass}>Experience max (yrs)</label><input id="f-emax" type="number" min={0} max={40} value={form.exp_max} onChange={num("exp_max")} className={inputClass} /></div>
        </div>
        <fieldset className="m-0 flex flex-wrap items-center gap-4 border-0 p-0">
          <legend className={labelClass}>Sources</legend>
          {["naukri", "linkedin"].map((s) => (
            <label key={s} className="flex items-center gap-1.5 text-[13px] capitalize">
              <input type="checkbox" checked={form.sources.includes(s)} onChange={() => toggle(s)} className="accent-[var(--accent)]" />{s}
            </label>
          ))}
        </fieldset>
        <div className="grid grid-cols-4 gap-3">
          <div><label htmlFor="f-cards" className={labelClass}>Cards / query</label><input id="f-cards" type="number" min={1} value={form.max_cards_per_query} onChange={num("max_cards_per_query")} className={inputClass} /></div>
          <div><label htmlFor="f-pages" className={labelClass}>Pages / query</label><input id="f-pages" type="number" min={1} value={form.max_pages_per_query} onChange={num("max_pages_per_query")} className={inputClass} /></div>
          <div><label htmlFor="f-enrich" className={labelClass}>Open / query</label><input id="f-enrich" type="number" min={0} value={form.enrich_per_query} onChange={num("enrich_per_query")} className={inputClass} /></div>
          <div><label htmlFor="f-backlog" className={labelClass}>Backlog / run</label><input id="f-backlog" type="number" min={0} value={form.backlog_budget} onChange={num("backlog_budget")} className={inputClass} /></div>
        </div>
        <p className="m-0 text-[12px] text-muted">
          Only India is searched. The experience range is a hard filter before Gemini scores a job; jobs that do not state years pass.
          "Open / query" is how many full job descriptions are loaded per search; the rest are saved as cards and filled in by the backlog on later runs.
        </p>
        <div><Button variant="primary" type="submit" disabled={save.isPending || form.sources.length === 0}>Save filters</Button></div>
      </form>
    </Panel>
  );
}
