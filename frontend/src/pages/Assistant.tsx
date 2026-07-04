import { useEffect, useRef, useState } from "react";
import { api, rpc, ApiError } from "../lib/api";
import { Card, Button, Badge, Banner } from "../components/ui";

interface ChangeRow { change_id: string; title: string; state: string }
interface ChangesResp { changes: ChangeRow[] }

interface RagResult {
  answer: string | null;
  confidence: number;
  suggest_escalation: boolean;
  chunk_change_id: string | null;
  citations?: unknown[] | null;
  message?: string;
}

interface Turn {
  question: string;
  result?: RagResult;
  error?: string;
  pending?: boolean;
}

function citationLabel(c: unknown): string {
  if (typeof c === "string") return c;
  if (c && typeof c === "object") {
    const o = c as Record<string, unknown>;
    return String(o.clause_ref ?? o.source ?? o.title ?? JSON.stringify(o));
  }
  return String(c);
}

export default function Assistant() {
  const [changes, setChanges] = useState<ChangeRow[]>([]);
  const [changeId, setChangeId] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    api
      .get<ChangesResp>("/admin/changes")
      .then((r) => setChanges(r.changes))
      .catch((e) => setLoadErr(e instanceof ApiError ? e.message : "Failed to load changes."));
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  // Grow the composer with the text, up to a cap; shrink back when cleared.
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [question]);

  // Reset the conversation when the operator switches change context.
  function onChangeSelect(id: string) {
    setChangeId(id);
    setTurns([]);
  }

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || !changeId || busy) return;
    setQuestion("");
    setBusy(true);
    const idx = turns.length;
    setTurns((t) => [...t, { question: q, pending: true }]);
    try {
      const result = await rpc<RagResult>("agent.query_rag", { query: q, change_id: changeId });
      setTurns((t) => t.map((tn, i) => (i === idx ? { question: q, result } : tn)));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Query failed.";
      setTurns((t) => t.map((tn, i) => (i === idx ? { question: q, error: msg } : tn)));
    } finally {
      setBusy(false);
    }
  }

  async function escalate(q: string, chunkChangeId?: string | null) {
    try {
      const res = await rpc<{ ticket_id?: string; status?: string }>("agent.escalate_to_npci", {
        query: q,
        session_id: crypto.randomUUID(),
        change_id: changeId,
        chunk_change_id: chunkChangeId ?? undefined,
        context: { change_id: changeId },
      });
      const ticket = res?.ticket_id ? ` Ticket ${res.ticket_id}.` : "";
      setTurns((t) => [
        ...t,
        {
          question: `(raised to NPCI) ${q}`,
          result: {
            answer:
              `Query raised to NPCI.${ticket} When NPCI responds, the answer will appear here ` +
              `and be added to the knowledge base automatically.`,
            confidence: 1,
            suggest_escalation: false,
            chunk_change_id: changeId,
          },
        },
      ]);
    } catch (err) {
      setTurns((t) => [...t, { question: q, error: err instanceof ApiError ? err.message : "Escalation failed." }]);
    }
  }

  const selected = changes.find((c) => c.change_id === changeId);

  return (
    <div className="flex h-full flex-col">
      <h1 className="mb-1 text-2xl font-semibold text-slate-900">Assistant</h1>
      <p className="mb-4 text-sm text-slate-500">
        Ask questions grounded in a single change&apos;s documents. Pick a change, then ask.
      </p>

      {loadErr && <div className="mb-4"><Banner kind="error">{loadErr}</Banner></div>}

      <div className="mb-4 flex items-center gap-3">
        <label className="text-sm font-medium text-slate-600">Change</label>
        <select
          value={changeId}
          onChange={(e) => onChangeSelect(e.target.value)}
          className="min-w-[22rem] rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600"
        >
          <option value="">Select a change…</option>
          {changes.map((c) => (
            <option key={c.change_id} value={c.change_id}>
              {c.change_id} — {c.title}
            </option>
          ))}
        </select>
        {selected && <Badge value={selected.state} />}
      </div>

      <Card className="flex min-h-0 flex-1 flex-col p-0">
        <div className="min-h-[16rem] flex-1 space-y-4 overflow-auto p-5">
          {turns.length === 0 && (
            <div className="grid h-full place-items-center text-sm text-slate-400">
              {changeId ? "Ask your first question about this change." : "Select a change to begin."}
            </div>
          )}
          {turns.map((t, i) => (
            <div key={i} className="space-y-2">
              <div className="flex justify-end">
                <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-brand-600 px-4 py-2 text-sm text-white">
                  {t.question}
                </div>
              </div>
              <div className="flex justify-start">
                <div className="max-w-[80%] rounded-2xl rounded-bl-sm bg-slate-100 px-4 py-2 text-sm text-slate-800">
                  {t.pending && <span className="text-slate-400">Thinking…</span>}
                  {t.error && <span className="text-red-600">{t.error}</span>}
                  {t.result && <AnswerBlock result={t.result} onEscalate={() => escalate(t.question, t.result?.chunk_change_id)} />}
                </div>
              </div>
            </div>
          ))}
          <div ref={endRef} />
        </div>

        <form onSubmit={ask} className="border-t border-slate-100 p-3">
          <div
            className={`flex items-end gap-2 rounded-2xl border p-1.5 pl-3.5 shadow-sm transition ${
              !changeId || busy
                ? "border-slate-200 bg-slate-50"
                : "border-slate-300 bg-white focus-within:border-brand-600 focus-within:ring-2 focus-within:ring-brand-100"
            }`}
          >
            <textarea
              ref={taRef}
              rows={1}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(e); }
              }}
              placeholder={changeId ? "Ask about this change…" : "Select a change first"}
              disabled={!changeId || busy}
              className="block max-h-40 flex-1 resize-none self-center bg-transparent py-1.5 text-sm leading-6 text-slate-800 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed"
            />
            <button
              type="submit"
              disabled={!changeId || busy || !question.trim()}
              aria-label="Send"
              className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-brand-600 text-white transition hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? (
                <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                </svg>
              ) : (
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 19V5M5 12l7-7 7 7" />
                </svg>
              )}
            </button>
          </div>
          <div className="mt-1.5 px-1 text-[11px] text-slate-400">
            <span className="font-medium text-slate-500">Enter</span> to send ·{" "}
            <span className="font-medium text-slate-500">Shift+Enter</span> for a new line
          </div>
        </form>
      </Card>
    </div>
  );
}

function AnswerBlock({ result, onEscalate }: { result: RagResult; onEscalate: () => void }) {
  const pct = Math.round((result.confidence ?? 0) * 100);
  if (result.suggest_escalation || !result.answer) {
    return (
      <div className="space-y-2">
        <div className="whitespace-pre-wrap text-slate-600">
          {result.answer || result.message || "No confident answer was found in this change's documents."}
        </div>
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
          <div className="mb-1.5 text-xs text-amber-800">
            The documents for this change don&apos;t answer this clearly. You can raise it as an official
            query — NPCI&apos;s reply will be added to the knowledge base automatically.
          </div>
          <Button variant="ghost" onClick={onEscalate}>Raise query to NPCI</Button>
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <div className="whitespace-pre-wrap">{result.answer}</div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
        <span>confidence {pct}%</span>
        {result.citations?.map((c, i) => (
          <span key={i} className="rounded bg-slate-200 px-1.5 py-0.5 text-slate-600">{citationLabel(c)}</span>
        ))}
      </div>
    </div>
  );
}
