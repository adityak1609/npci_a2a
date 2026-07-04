import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Card, Button, Input, Textarea, Label, Banner } from "../components/ui";

interface KbDoc {
  title: string;
  label?: string;
  source?: string;
  source_label?: string;
  chunks: number;
  summary: string | null;
  created_at: string | null;
}

export default function KnowledgeBase() {
  const { me } = useAuth();
  const canEdit = !!me?.is_admin;
  const [docs, setDocs] = useState<KbDoc[]>([]);
  const [form, setForm] = useState({ title: "", source: "", content: "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  async function load() {
    const r = await api.get<{ documents: KbDoc[] }>("/admin/kb/documents");
    setDocs(r.documents);
  }
  useEffect(() => { load(); }, []);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    setBusy(true);
    try {
      const r = await api.post<{ chunks_indexed: number }>("/admin/kb/documents", form);
      setForm({ title: "", source: "", content: "" });
      await load();
      setMsg({ kind: "success", text: `Indexed ${r.chunks_indexed} chunks.` });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Indexing failed." });
    } finally {
      setBusy(false);
    }
  }

  async function remove(title: string) {
    try {
      await api.del(`/admin/kb/documents/${encodeURIComponent(title)}`);
      await load();
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Delete failed." });
    }
  }

  return (
    <div>
      <h1 className="mb-1 text-2xl font-semibold text-slate-900">Knowledge Base</h1>
      <p className="mb-6 text-sm text-slate-500">
        Specs, NPCI circulars, past change kits, and internal standards. These are indexed and retrieved by the RAG / Design / Code / Test agents.
      </p>

      {msg && <div className="mb-4"><Banner kind={msg.kind}>{msg.text}</Banner></div>}

      {canEdit && (
        <Card className="mb-8 p-6">
          <div className="mb-3 text-sm font-semibold text-slate-700">Add a document</div>
          <form onSubmit={add} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div><Label>Title</Label><Input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="e.g. UPI 2.0 Technical Spec" required /></div>
              <div><Label>Source (optional)</Label><Input value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} placeholder="filename / URL" /></div>
            </div>
            <div>
              <Label>Content</Label>
              <Textarea rows={8} value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} placeholder="Paste the document content (markdown or plain text)…" required />
            </div>
            <Button type="submit" disabled={busy}>{busy ? "Indexing…" : "Add + Index"}</Button>
          </form>
        </Card>
      )}

      <div className="mb-3 text-sm font-semibold text-slate-700">Documents</div>
      {docs.length === 0 ? (
        <div className="text-sm text-slate-400">No documents yet. Add one above to seed the knowledge base.</div>
      ) : (
        <div className="space-y-2">
          {docs.map((d) => (
            <Card key={d.title} className="flex items-center justify-between p-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-800">{d.label ?? d.title}</span>
                  {d.source_label && (
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-500">
                      {d.source_label}
                    </span>
                  )}
                </div>
                <div className="truncate text-xs text-slate-400">{d.chunks} chunks · {d.summary?.slice(0, 80)}</div>
              </div>
              {canEdit && <Button variant="danger" onClick={() => remove(d.title)}>Delete</Button>}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
