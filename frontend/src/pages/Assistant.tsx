import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type Explanation } from "../lib/api";
import { ErrorBox, Panel, Pill } from "../components/ui";
import { ExplanationView } from "./SignalDetail";

interface AskResp { question: string; intent: string; symbol: string | null; answer: Explanation; provider: string; suggested: string[] }
interface MetaResp { suggested: string[]; llm: { active_provider: string; model: string | null; note: string }; guardrails: string[] }

export default function Assistant() {
  const meta = useQuery({ queryKey: ["assistant-meta"], queryFn: () => api.get<MetaResp>("/assistant/meta") });
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<AskResp[]>([]);
  const ask = useMutation({
    mutationFn: (q: string) => api.post<AskResp>("/assistant/ask", { question: q }),
    onSuccess: (r) => { setHistory((h) => [r, ...h]); setQuestion(""); },
  });
  const submit = (q: string) => { if (q.trim().length >= 2) ask.mutate(q.trim()); };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">AI Research Assistant</h1>
        {meta.data && <Pill>{meta.data.llm.active_provider}{meta.data.llm.model ? ` · ${meta.data.llm.model}` : ""}</Pill>}
        {meta.data && <span className="text-xs text-slate-500">{meta.data.llm.note}</span>}
      </div>
      <Panel>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); submit(question); }}>
          <input className="input flex-1 text-sm py-2" placeholder="Ask about a symbol, strategy, regime, relative strength or out-of-sample results…" value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={500} />
          <button className="btn btn-primary" disabled={ask.isPending || question.trim().length < 2}>{ask.isPending ? "Thinking…" : "Ask"}</button>
        </form>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {(meta.data?.suggested ?? []).map((s) => <button key={s} className="btn" onClick={() => submit(s)}>{s}</button>)}
        </div>
        {meta.data && <ul className="mt-2 text-[10px] text-slate-500 space-y-0.5">{meta.data.guardrails.map((g, i) => <li key={i}>• {g}</li>)}</ul>}
      </Panel>
      {ask.error && <ErrorBox error={ask.error} />}
      {history.map((h, i) => (
        <Panel key={i} title={<span className="normal-case tracking-normal text-slate-200">Q: {h.question}</span>}
          right={<span className="flex items-center gap-2"><Pill>intent: {h.intent}</Pill><Pill>{h.provider}</Pill>
            {h.symbol && <Link className="text-sky-300 hover:underline" to={`/charts/NSE/${encodeURIComponent(h.symbol)}`}>chart</Link>}</span>}>
          <ExplanationView e={h.answer} />
          {h.answer.sources.length > 0 && <div className="mt-2 text-[10px] text-slate-500">Sources: {h.answer.sources.join(", ")}</div>}
        </Panel>
      ))}
    </div>
  );
}
