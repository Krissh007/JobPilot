import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Settings } from "../api";
import { useSchedule } from "../data";
import { useUI } from "../state";
import { Button, Panel, cx, inputClass, labelClass, when } from "../ui";

const DAY = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" } as Record<string, string>;

function runTimes(start: string, end: string, every: number): string[] {
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  const out: string[] = [];
  for (let m = sh * 60 + sm; m <= eh * 60 + em && out.length < 24; m += every * 60)
    out.push(`${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`);
  return out;
}

export function SchedulePanel({ settings }: { settings: Settings }) {
  const { data: status, isFetching } = useSchedule();
  const { toast } = useUI();
  const qc = useQueryClient();
  const [form, setForm] = useState(settings.schedule);
  const [changed, setChanged] = useState(false);
  useEffect(() => setForm(settings.schedule), [settings.schedule]);

  const registered = status?.tasks.some((t) => t.registered) ?? false;
  const save = useMutation({
    mutationFn: () => api.saveSettings({ schedule: form }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["schedule"] }); setChanged(registered); toast(registered ? "Saved. Click Update tasks to apply it to Windows." : "Schedule saved"); },
    onError: (e: Error) => toast(`Not saved: ${e.message}`),
  });
  const apply = useMutation({
    mutationFn: (on: boolean) => api.registerSchedule(on),
    onSuccess: (r, on) => { qc.setQueryData(["schedule"], r); setChanged(false); toast(on ? "Windows tasks registered" : "Windows tasks removed"); },
    onError: (e: Error) => toast(e.message),
  });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };
  const times = runTimes(form.start, form.end, form.every_hours);
  const label = (name: string) => (name === status?.config.tasks.pipeline ? "Search → score → docs" : "Weekly answer review");

  return (
    <Panel title="Schedule" actions={<span className="text-[12px] text-muted">{isFetching ? "checking…" : status ? `checked ${when(status.checked_at)}` : ""}</span>}>
      <div className="flex flex-col gap-3 p-3.5 text-[12.5px]">
        <form onSubmit={submit} className="grid grid-cols-[1fr_1fr_1fr_auto] items-end gap-2">
          <div><label htmlFor="sch-every" className={labelClass}>Every (h)</label><input id="sch-every" type="number" min={1} max={24} value={form.every_hours} onChange={(e) => setForm({ ...form, every_hours: Number(e.target.value) })} className={inputClass} /></div>
          <div><label htmlFor="sch-start" className={labelClass}>From (IST)</label><input id="sch-start" type="time" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} className={inputClass} /></div>
          <div><label htmlFor="sch-end" className={labelClass}>Until</label><input id="sch-end" type="time" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} className={inputClass} /></div>
          <Button type="submit" disabled={save.isPending}>Save</Button>
        </form>
        <p className="m-0 text-muted">Runs at <b className="num text-fg">{times.join(", ") || "—"}</b>: search, then score, then docs. Apply is never scheduled (it opens a browser and waits for you). Weekly reminder: <b className="text-fg">{DAY[settings.weekly_review.day]} {settings.weekly_review.time}</b>.</p>

        <div className="overflow-x-auto rounded-md border border-line">
          <table className="w-full min-w-[420px] border-collapse">
            <thead><tr className="border-b border-line bg-sunk/70 text-left text-[11px] uppercase tracking-[.06em] text-muted"><th className="px-2.5 py-1.5">Windows task</th><th className="px-2.5 py-1.5">Status</th><th className="px-2.5 py-1.5">Next run</th><th className="px-2.5 py-1.5">Last run</th></tr></thead>
            <tbody>
              {(status?.tasks ?? []).map((t) => (
                <tr key={t.name} className="border-b border-line last:border-0">
                  <td className="px-2.5 py-1.5">{label(t.name)}<div className="font-mono text-[11px] text-muted">{t.name}</div></td>
                  <td className="px-2.5 py-1.5">
                    <span className={cx("rounded px-1.5 text-[11px] font-semibold", !t.registered ? "bg-sunk text-muted" : t.state === "Disabled" ? "bg-warn-soft text-warn" : "bg-good-soft text-good")}>
                      {t.registered ? (t.state ?? "").toLowerCase() : "not registered"}
                    </span>
                  </td>
                  <td className="num px-2.5 py-1.5">{t.registered ? when(t.next_run) : "—"}</td>
                  <td className="px-2.5 py-1.5"><span className="num">{t.registered ? when(t.last_run) : "—"}</span>
                    {t.last_result_text && t.last_run && <span className={cx("ml-1.5 text-[11.5px]", t.last_result_text === "ok" ? "text-good" : t.last_result_text.startsWith("error") || t.last_result_text.startsWith("finished") ? "text-bad" : "text-muted")}>{t.last_result_text}</span>}</td>
                </tr>
              ))}
              {!status && <tr><td colSpan={4} className="px-2.5 py-3 text-muted">Reading Task Scheduler…</td></tr>}
            </tbody>
          </table>
        </div>
        {status?.error && <p className="m-0 text-bad">Couldn't read Task Scheduler: {status.error}</p>}
        {changed && <p role="note" className="m-0 rounded-md border border-warn/40 bg-warn-soft p-2 text-warn">Times changed. Click <b>Update tasks</b> so Windows uses them.</p>}

        <div className="flex flex-wrap items-center gap-1.5">
          <Button variant="primary" disabled={apply.isPending || !status} onClick={() => apply.mutate(true)}>{registered ? "Update tasks" : "Register with Windows"}</Button>
          {registered && <Button disabled={apply.isPending} onClick={() => apply.mutate(false)}>Remove tasks</Button>}
          <span className="text-[11.5px] text-muted">Runs only while you're logged on; a missed run starts when the PC is back. No admin rights needed.</span>
        </div>
        <details className="text-[12px] text-muted">
          <summary className="cursor-pointer">Same thing from a terminal, and the last scheduled log</summary>
          <code className="mt-1.5 block break-all rounded bg-sunk px-2 py-1.5 font-mono text-[11.5px] text-fg">{status?.command}</code>
          <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-sunk px-2 py-1.5 font-mono text-[11px]">{status?.log_tail.length ? status.log_tail.join("\n") : "No scheduled pass has run yet."}</pre>
        </details>
      </div>
    </Panel>
  );
}
