import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, rpc, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Card, Button, Input, Textarea, Label, Badge, Banner } from "../components/ui";

interface Detail {
  change: { change_id: string; title: string; state: string; bucket: string; version: number | null; blocked: boolean; blocked_reason: string | null };
  documents: { id: number; filename: string; doc_type: string | null; status: string; version: number | null; summary: string | null }[];
  counter_proposals: { counter_proposal_id: string; status: string; decided: boolean }[];
  blockers: { blocker_id: string; type: string | null; severity: string | null; status: string; resolution: string | null }[];
  milestones: { milestone: string; state: string; eta: string | null }[];
  tickets: { ticket_id: string; query: string; status: string; answer: string | null }[];
  owner: { id: number; username: string } | null;
}

interface UserRow { id: number; username: string; role: string }

type ActionKey = "readiness" | "blocker" | "counter" | "milestone" | null;

export default function ChangeDetail() {
  const { changeId = "" } = useParams();
  const { me } = useAuth();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [action, setAction] = useState<ActionKey>(null);
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  async function load() {
    const d = await api.get<Detail>(`/admin/changes/${encodeURIComponent(changeId)}`);
    setDetail(d);
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [changeId]);

  useEffect(() => {
    if (me?.is_admin) {
      api.get<{ users: UserRow[] }>("/admin/users").then((r) => setUsers(r.users)).catch(() => {});
    }
  }, [me?.is_admin]);

  async function assignOwner(userId: number | null) {
    setMsg(null);
    try {
      await api.post(`/admin/changes/${encodeURIComponent(changeId)}/assign`, { user_id: userId });
      await load();
      setMsg({ kind: "success", text: userId === null ? "Owner cleared." : "Owner assigned." });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Assign failed." });
    }
  }

  async function run(method: string, params: Record<string, unknown>) {
    setMsg(null);
    try {
      await rpc(method, { change_id: changeId, ...params });
      setMsg({ kind: "success", text: "Message sent to NPCI." });
      setAction(null);
      await load();
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Failed to send." });
    }
  }

  if (!detail) return <div className="text-slate-400">Loading…</div>;
  const c = detail.change;

  return (
    <div>
      <Link to="/" className="text-sm text-slate-500 hover:text-slate-800">← Back to changes</Link>
      <div className="mt-2 mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">{c.title}</h1>
          <div className="mt-1 flex items-center gap-2 text-sm text-slate-500">
            <span>{c.change_id}</span> · <span>{c.state}</span> · <Badge value={c.bucket} />
            {c.blocked && <span className="text-red-500">● blocked: {c.blocked_reason}</span>}
          </div>
        </div>
        {me?.is_admin && (
          <div className="flex gap-2">
            <Button variant="ghost" onClick={() => setAction("milestone")}>Report milestone</Button>
            <Button variant="ghost" onClick={() => setAction("counter")}>Submit counter-proposal</Button>
            <Button variant="ghost" onClick={() => setAction("blocker")}>Raise blocker</Button>
            <Button onClick={() => setAction("readiness")}>Declare readiness</Button>
          </div>
        )}
      </div>

      {me?.is_admin && (
        <Card className="mb-4 flex items-center justify-between p-4">
          <div className="text-sm text-slate-600">
            Concerned user:{" "}
            <span className="font-medium text-slate-800">
              {detail.owner ? detail.owner.username : "Unassigned"}
            </span>
            <div className="text-xs text-slate-400">
              Only this user sees NPCI updates for this change on their dashboard.
            </div>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={detail.owner?.id ?? ""}
              onChange={(e) => assignOwner(e.target.value ? Number(e.target.value) : null)}
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600"
            >
              <option value="">Unassigned</option>
              {users.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.username}{u.role === "admin" ? " (admin)" : ""}
                </option>
              ))}
            </select>
          </div>
        </Card>
      )}

      {msg && <div className="mb-4"><Banner kind={msg.kind}>{msg.text}</Banner></div>}
      {action && <ActionForm action={action} onCancel={() => setAction(null)} onRun={run} />}

      <div className="grid grid-cols-2 gap-6">
        <Section title="Documents & attachments from NPCI">
          {detail.documents.length === 0 ? <Empty /> : detail.documents.map((d) => (
            <div key={d.id} className="border-b border-slate-50 pb-3 last:border-0">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm text-slate-700">{d.filename}</div>
                  {d.doc_type && <div className="text-xs text-slate-400">{d.doc_type}</div>}
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <a
                    href={`/documents/${d.id}/download`}
                    download
                    className="text-xs font-medium text-brand-600 hover:underline"
                  >
                    Download
                  </a>
                  <Badge value={d.status} />
                </div>
              </div>
              <div className="mt-2">
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">Summary</div>
                {d.summary ? (
                  <div className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs leading-relaxed text-slate-600">
                    {d.summary}
                  </div>
                ) : (
                  <div className="rounded-lg bg-slate-50 p-3 text-xs text-slate-400">
                    {d.status === "ready"
                      ? "No summary available for this document."
                      : `Summary is being generated (status: ${d.status})…`}
                  </div>
                )}
              </div>
            </div>
          ))}
        </Section>
        <Section title="Milestones">
          {detail.milestones.length === 0 ? <Empty /> : detail.milestones.map((m) => (
            <Row key={m.milestone} left={m.milestone} right={<Badge value={m.state} />} sub={m.eta ?? undefined} />
          ))}
        </Section>
        <Section title="Counter-proposals">
          {detail.counter_proposals.length === 0 ? <Empty /> : detail.counter_proposals.map((cp) => (
            <Row key={cp.counter_proposal_id} left={cp.counter_proposal_id} right={<Badge value={cp.decided ? "resolved" : "open"} />} />
          ))}
        </Section>
        <Section title="Blockers">
          {detail.blockers.length === 0 ? <Empty /> : detail.blockers.map((b) => (
            <Row key={b.blocker_id} left={b.blocker_id} right={<Badge value={b.resolution ?? b.status} />} sub={`${b.type ?? ""} ${b.severity ?? ""}`} />
          ))}
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card className="p-5">
      <div className="mb-3 text-sm font-semibold text-slate-700">{title}</div>
      <div className="space-y-2">{children}</div>
    </Card>
  );
}
function Row({ left, right, sub }: { left: string; right?: React.ReactNode; sub?: string }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-50 pb-2 last:border-0">
      <div><div className="text-sm text-slate-700">{left}</div>{sub && <div className="text-xs text-slate-400">{sub}</div>}</div>
      {right}
    </div>
  );
}
function Empty() {
  return <div className="text-xs text-slate-400">None yet.</div>;
}

function ActionForm({
  action,
  onCancel,
  onRun,
}: {
  action: Exclude<ActionKey, null>;
  onCancel: () => void;
  onRun: (method: string, params: Record<string, unknown>) => void;
}) {
  const [f, setF] = useState<Record<string, string>>({});
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setF((p) => ({ ...p, [k]: e.target.value }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (action === "readiness") {
      onRun("agent.declare_readiness", {
        version_implementing: Number(f.version || 1),
        implementation_summary: f.summary || "",
      });
    } else if (action === "blocker") {
      onRun("agent.raise_blocker", {
        type: f.type || "other",
        severity: f.severity || "medium",
        subject: f.subject || "",
        description: f.description || "",
      });
    } else if (action === "milestone") {
      onRun("agent.report_milestone", {
        milestone: f.milestone || "design",
        state: f.state || "completed",
        version_implementing: Number(f.version || 1),
        notes: f.notes || "",
      });
    } else if (action === "counter") {
      onRun("agent.submit_counter_proposal", {
        summary: f.summary || "",
        counters: [{ clause_ref: f.clause_ref || "", proposed_text: f.proposed_text || "", rationale: f.rationale || "" }],
      });
    }
  }

  const TITLES = {
    readiness: "Declare readiness",
    blocker: "Raise blocker",
    milestone: "Report milestone",
    counter: "Submit counter-proposal",
  };

  return (
    <Card className="mb-6 p-5">
      <div className="mb-3 text-sm font-semibold text-slate-700">{TITLES[action]}</div>
      <form onSubmit={submit} className="space-y-3">
        {action === "readiness" && (
          <>
            <div><Label>Version implementing</Label><Input value={f.version || ""} onChange={set("version")} inputMode="numeric" /></div>
            <div><Label>Implementation summary</Label><Textarea rows={3} value={f.summary || ""} onChange={set("summary")} /></div>
          </>
        )}
        {action === "blocker" && (
          <>
            <div><Label>Type</Label><Input value={f.type || ""} onChange={set("type")} placeholder="simulator_issue" /></div>
            <div><Label>Severity</Label><Input value={f.severity || ""} onChange={set("severity")} placeholder="high" /></div>
            <div><Label>Subject</Label><Input value={f.subject || ""} onChange={set("subject")} /></div>
            <div><Label>Description</Label><Textarea rows={3} value={f.description || ""} onChange={set("description")} /></div>
          </>
        )}
        {action === "milestone" && (
          <>
            <div><Label>Milestone</Label><Input value={f.milestone || ""} onChange={set("milestone")} placeholder="design | coding | testing" /></div>
            <div><Label>State</Label><Input value={f.state || ""} onChange={set("state")} placeholder="completed" /></div>
            <div><Label>Version implementing</Label><Input value={f.version || ""} onChange={set("version")} inputMode="numeric" /></div>
            <div><Label>Notes</Label><Textarea rows={2} value={f.notes || ""} onChange={set("notes")} /></div>
          </>
        )}
        {action === "counter" && (
          <>
            <div><Label>Clause ref</Label><Input value={f.clause_ref || ""} onChange={set("clause_ref")} placeholder="TSD §4.2.3" /></div>
            <div><Label>Proposed text</Label><Textarea rows={2} value={f.proposed_text || ""} onChange={set("proposed_text")} /></div>
            <div><Label>Rationale</Label><Textarea rows={2} value={f.rationale || ""} onChange={set("rationale")} /></div>
            <div><Label>Summary</Label><Input value={f.summary || ""} onChange={set("summary")} /></div>
          </>
        )}
        <div className="flex gap-2 pt-1">
          <Button type="submit">Send to NPCI</Button>
          <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        </div>
      </form>
    </Card>
  );
}
