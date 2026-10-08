import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Application } from "../api";
import { useApplications } from "../data";
import { useUI } from "../state";
import { Button, Empty, ExampleTag, Panel, ScoreNum, ago, cx, when } from "../ui";

const SOURCE_LABEL = { resume: "resume", answer_bank: "answer bank", llm: "Gemini draft" } as const;

function Card({ app, threshold }: { app: Application; threshold: number }) {
  const { openJob, toast } = useUI();
  const qc = useQueryClient();
  const decide = useMutation({
    mutationFn: (d: "approve" | "skip") => api.decide(app.id, d),
    onSuccess: (a) => { toast(a.state === "approved" ? "Approved. The apply agent will submit it in its browser." : "Skipped."); qc.invalidateQueries({ queryKey: ["applications"] }); },
    onError: (e: Error) => toast(e.message),
  });
  const flagged = app.needs_review;
  const incomplete = app.reason === "incomplete";
  const naukri = app.job?.source === "naukri";
  return (
    <Panel
      title={<span className="flex items-center gap-2">{app.job?.title} · {app.job?.company} <ScoreNum score={app.job?.score ?? null} threshold={threshold} />{app.is_example && <ExampleTag />}</span>}
      actions={<span className="text-[12px] text-muted">paused {ago(app.created_at)} ago</span>}>
      <div className="grid gap-4 p-3.5 lg:grid-cols-[1.25fr_1fr]">
        <div className="flex min-w-0 flex-col gap-3">
          {incomplete && (
            <div role="note" className="rounded-md border border-warn/40 bg-warn-soft p-2.5 text-[12.5px]">
              <b className="text-warn">Not ready to submit.</b> {naukri ? "Naukri's screening chat asked something your answer bank can't answer (or a consent question). Answer it in the agent's Chrome window; JobPilot records the application when Naukri confirms it." : "The form is stuck before LinkedIn's Submit page: the highlighted rows need you (consent boxes are never ticked by the agent). Finish them in the agent's Chrome window. Approve unlocks once the form reaches Submit, or submit it there yourself and JobPilot records it."}
            </div>
          )}
          {naukri && !incomplete && (
            <p className="m-0 rounded-md border border-line-2 bg-sunk px-2.5 py-2 text-[12.5px]"><b>Naukri:</b> nothing has been sent yet. Approve clicks Naukri's Apply, which sends your Naukri profile and resume; any screening questions are answered only from your answer bank.</p>
          )}
          <p className="m-0 text-[12.5px] text-muted">
            {flagged ? <b className="text-warn">{flagged} answer{flagged > 1 ? "s" : ""} drafted by Gemini need your eye (highlighted).</b> : "Every answer came from your resume or answer bank."}
          </p>
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full min-w-[420px] border-collapse text-[12.5px]">
              <thead><tr className="border-b border-line bg-sunk/70 text-left text-[11px] uppercase tracking-[.06em] text-muted"><th className="px-2.5 py-1.5">Step</th><th className="px-2.5 py-1.5">Field</th><th className="px-2.5 py-1.5">Value used</th><th className="px-2.5 py-1.5">From</th></tr></thead>
              <tbody>
                {app.fields.map((f) => (
                  <tr key={f.id} className={cx("border-b border-line last:border-0", f.needs_review && "bg-warn-soft")}>
                    <td className="num px-2.5 py-1.5 text-muted">{f.step}</td><td className="px-2.5 py-1.5">{f.field_name}</td>
                    <td className="px-2.5 py-1.5">{f.value_used}</td>
                    <td className={cx("whitespace-nowrap px-2.5 py-1.5", f.needs_review ? "font-medium text-warn" : "text-muted")}>{SOURCE_LABEL[f.source]}</td>
                  </tr>
                ))}
                {app.fields.length === 0 && <tr><td colSpan={4} className="px-2.5 py-3 text-muted">No fields recorded.</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Button variant="primary" disabled={decide.isPending || incomplete} title={incomplete ? "Finish the form in the browser window first" : undefined} onClick={() => decide.mutate("approve")}>Approve submit</Button>
            <Button disabled={decide.isPending} onClick={() => decide.mutate("skip")}>Skip</Button>
            {app.job && <Button variant="ghost" onClick={() => openJob(app.job!.id)}>Open job</Button>}
          </div>
        </div>
        <div className="grid aspect-[4/3] place-items-center overflow-hidden rounded-md border border-dashed border-line-2 bg-[repeating-linear-gradient(45deg,var(--sunk)_0_10px,transparent_10px_20px)] text-center text-[12.5px] text-muted">
          {app.has_screenshot
            ? <img src={`/api/applications/${app.id}/screenshot`} alt={`Filled form for ${app.job?.company}`} className="h-full w-full object-contain object-top" />
            : <span className="px-6">The apply agent saves a screenshot of the filled form here before pausing.</span>}
        </div>
      </div>
    </Panel>
  );
}

export function Review({ threshold }: { threshold: number }) {
  const { data: apps, isLoading } = useApplications();
  if (isLoading) return <Empty title="Loading the review queue…" />;
  const pending = apps?.filter((a) => a.state === "pending_review") ?? [];
  const blocked = apps?.filter((a) => a.state === "blocked") ?? [];
  const decided = apps?.filter((a) => !["pending_review", "blocked", "filling"].includes(a.state)) ?? [];

  return (
    <div className="flex flex-col gap-3">
      <p className="m-0 text-[12.5px] text-muted">The apply agent fills each form, saves a screenshot and <b className="text-fg">stops before Submit</b>. Approve here and it submits in the same browser tab, as long as it is still holding the form open. Skip drops it. Applications land here when auto-submit is off, Gemini drafted an answer, or a required field is empty.</p>
      {pending.length ? pending.map((a) => <Card key={a.id} app={a} threshold={threshold} />) : <Empty title="Nothing waiting for review">Use <b>Queue for apply</b> on a job, or <b>Run now</b> on the Apply card in Runs.</Empty>}
      {blocked.length > 0 && (
        <Panel title={<span className="text-bad">Blocked: needs you</span>}>
          <ul className="m-0 list-none p-0">
            {blocked.map((a) => (
              <li key={a.id} className="flex items-center gap-2 border-b border-line px-3.5 py-2 text-[12.5px] last:border-0">
                <span className="rounded bg-bad-soft px-1.5 text-[11px] font-semibold text-bad">blocked</span>
                {a.job?.title} · {a.job?.company}{a.is_example && <ExampleTag />}
                <span className="ml-auto text-muted">CAPTCHA, OTP or sign-in page. The agent stopped and left the browser on it. {when(a.created_at)}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
      {decided.length > 0 && (
        <Panel title="Decided">
          <table className="w-full border-collapse text-[12.5px]"><tbody>
            {decided.map((a) => (
              <tr key={a.id} className="border-b border-line last:border-0">
                <td className="px-3.5 py-1.5">{a.job?.title} · {a.job?.company}</td>
                <td className="px-3.5 py-1.5"><span className={cx("rounded px-1.5 text-[11px] font-semibold", a.state === "skipped" ? "bg-sunk text-muted" : "bg-good-soft text-good")}>{a.state}</span>{a.reason && <span className="ml-1.5 text-[11.5px] text-muted">{a.reason.replace(/_/g, " ")}</span>}</td>
                <td className="num px-3.5 py-1.5 text-right text-muted">{when(a.decided_at)}</td>
              </tr>
            ))}
          </tbody></table>
        </Panel>
      )}
    </div>
  );
}
