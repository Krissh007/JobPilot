import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Answer } from "../api";
import { useAnswers } from "../data";
import { useUI } from "../state";
import { Button, Empty, ExampleTag, Panel, inputClass, labelClass } from "../ui";

function Row({ a }: { a: Answer }) {
  const { toast } = useUI();
  const qc = useQueryClient();
  const [edit, setEdit] = useState<{ question: string; answer: string; tags: string } | null>(null);
  const [confirm, setConfirm] = useState(false);
  const done = (m: string) => { toast(m); qc.invalidateQueries({ queryKey: ["answers"] }); };
  const save = useMutation({ mutationFn: () => api.updateAnswer(a.id, edit!), onSuccess: () => { setEdit(null); done("Answer updated"); }, onError: (e: Error) => toast(e.message) });
  const remove = useMutation({ mutationFn: () => api.deleteAnswer(a.id), onSuccess: () => done("Answer deleted") });

  if (edit) return (
    <tr className="border-b border-line bg-accent-soft/40">
      <td className="px-3 py-2 align-top"><input aria-label="Question" value={edit.question} onChange={(e) => setEdit({ ...edit, question: e.target.value })} className={inputClass} /></td>
      <td className="px-3 py-2 align-top"><textarea aria-label="Answer" rows={2} value={edit.answer} onChange={(e) => setEdit({ ...edit, answer: e.target.value })} className={inputClass} /></td>
      <td className="px-3 py-2 align-top"><input aria-label="Tags" value={edit.tags} onChange={(e) => setEdit({ ...edit, tags: e.target.value })} className={inputClass} /></td>
      <td className="num px-3 py-2 align-top">{a.times_used}</td>
      <td className="whitespace-nowrap px-3 py-2 text-right align-top"><Button size="sm" variant="primary" onClick={() => save.mutate()}>Save</Button> <Button size="sm" variant="ghost" onClick={() => setEdit(null)}>Cancel</Button></td>
    </tr>
  );
  return (
    <tr className="border-b border-line last:border-0">
      <td className="px-3 py-2 font-semibold">{a.question} {a.is_example && <ExampleTag />}</td>
      <td className="px-3 py-2">{a.answer}</td>
      <td className="px-3 py-2">{a.tags.split(",").filter((t) => t.trim()).map((t) => <span key={t} className="mr-1 rounded bg-sunk px-1.5 text-[11.5px] text-muted">{t.trim()}</span>)}</td>
      <td className="num px-3 py-2">{a.times_used}</td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        {confirm
          ? <><Button size="sm" variant="danger" onClick={() => remove.mutate()}>Delete</Button> <Button size="sm" variant="ghost" onClick={() => setConfirm(false)}>Keep</Button></>
          : <><Button size="sm" variant="ghost" onClick={() => setEdit({ question: a.question, answer: a.answer, tags: a.tags })}>Edit</Button> <Button size="sm" variant="ghost" onClick={() => setConfirm(true)}>Delete</Button></>}
      </td>
    </tr>
  );
}

export function Answers() {
  const { data: answers, isLoading } = useAnswers();
  const { toast } = useUI();
  const qc = useQueryClient();
  const [form, setForm] = useState({ question: "", answer: "", tags: "" });
  const create = useMutation({
    mutationFn: () => api.createAnswer(form),
    onSuccess: () => { setForm({ question: "", answer: "", tags: "" }); toast("Answer saved"); qc.invalidateQueries({ queryKey: ["answers"] }); },
    onError: (e: Error) => toast(e.message),
  });
  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate(); };
  const uses = answers?.reduce((s, a) => s + a.times_used, 0) ?? 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 lg:grid-cols-[1.4fr_1fr]">
        <Panel title="Add a screening answer">
          <form onSubmit={submit} className="grid gap-3 p-3.5 sm:grid-cols-2">
            <div><label htmlFor="q" className={labelClass}>Question (or a phrase from it)</label><input id="q" required minLength={2} value={form.question} onChange={(e) => setForm({ ...form, question: e.target.value })} className={inputClass} placeholder="Notice period" /></div>
            <div><label htmlFor="tags" className={labelClass}>Tags</label><input id="tags" value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} className={inputClass} placeholder="notice, availability" /></div>
            <div className="sm:col-span-2"><label htmlFor="a" className={labelClass}>Answer the agent should type</label><textarea id="a" required rows={2} value={form.answer} onChange={(e) => setForm({ ...form, answer: e.target.value })} className={inputClass} /></div>
            <div><Button variant="primary" type="submit" disabled={create.isPending}>Save answer</Button></div>
          </form>
        </Panel>
        <Panel title="How the bank is used">
          <div className="p-3.5 text-[12.5px] text-muted">
            <p className="mt-0">The apply agent matches each form question against these answers first (the longest matching phrase wins). Only unmatched questions get a Gemini draft, and those are flagged in the review queue before anything is submitted.</p>
            <p className="num mb-0">{answers?.length ?? 0} answers · {uses} uses</p>
          </div>
        </Panel>
      </div>
      {isLoading ? <Empty title="Loading answers…" /> : answers?.length ? (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full min-w-[760px] border-collapse text-[12.5px]">
            <thead><tr className="border-b border-line bg-sunk/70 text-left text-[11px] uppercase tracking-[.06em] text-muted">
              <th className="w-[28%] px-3 py-2">Question</th><th className="px-3 py-2">Answer</th><th className="w-[16%] px-3 py-2">Tags</th><th className="w-14 px-3 py-2">Used</th><th className="w-36 px-3 py-2" />
            </tr></thead>
            <tbody>{answers.map((a) => <Row key={a.id} a={a} />)}</tbody>
          </table>
        </div>
      ) : <Empty title="Answer bank is empty">Add the questions recruiters always ask: notice period, current CTC, relocation, years with SQL.</Empty>}
    </div>
  );
}
