import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { StatCard, Card, Input, Badge } from "../components/ui";

interface ChangeRow {
  change_id: string;
  title: string;
  state: string;
  bucket: string;
  version: number | null;
  blocked: boolean;
  doc_count: number;
  summary: string | null;
  updated_at: string;
}
interface ChangesResp {
  counts: { total: number; new: number; in_progress: number; certified: number };
  changes: ChangeRow[];
}

const FILTERS = [
  { key: "", label: "All" },
  { key: "new", label: "New" },
  { key: "in_progress", label: "In progress" },
  { key: "certified", label: "Certified" },
];

export default function Dashboard() {
  const [data, setData] = useState<ChangesResp | null>(null);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState("");

  async function load() {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (filter) params.set("status_filter", filter);
    const resp = await api.get<ChangesResp>(`/admin/changes?${params.toString()}`);
    setData(resp);
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 5000); // live auto-refresh
    return () => clearInterval(t);
  }, [q, filter]);

  const counts = data?.counts ?? { total: 0, new: 0, in_progress: 0, certified: 0 };
  const rows = useMemo(() => data?.changes ?? [], [data]);

  async function del(c: ChangeRow) {
    if (
      !window.confirm(
        `Delete change "${c.title}" (${c.change_id})?\n\n` +
          "It will be removed from the board, dashboards, and the Assistant. Its audit trail is kept.",
      )
    )
      return;
    try {
      await api.del(`/admin/changes/${encodeURIComponent(c.change_id)}`);
      await load();
    } catch (e) {
      alert(e instanceof ApiError ? e.message : "Delete failed.");
    }
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Incoming Changes</h1>
          <p className="text-sm text-slate-500">Changes published by NPCI appear here automatically.</p>
        </div>
        <span className="flex items-center gap-1.5 text-xs text-emerald-600">
          <span className="h-2 w-2 rounded-full bg-emerald-500" /> Live · auto-refresh
        </span>
      </div>

      <div className="mb-6 grid grid-cols-4 gap-4">
        <StatCard label="Total" value={counts.total} />
        <StatCard label="New" value={counts.new} />
        <StatCard label="In Progress" value={counts.in_progress} />
        <StatCard label="Certified" value={counts.certified} />
      </div>

      <div className="mb-4 flex items-center gap-3">
        <div className="flex-1">
          <Input placeholder="Search changes…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="flex gap-1 rounded-lg border border-slate-200 bg-white p-1">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded-md px-3 py-1 text-sm ${
                filter === f.key ? "bg-brand-50 text-brand-700" : "text-slate-500 hover:text-slate-800"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      <Card>
        {rows.length === 0 ? (
          <div className="py-16 text-center text-slate-400">
            <div className="text-sm font-medium text-slate-500">No changes received yet</div>
            <div className="text-xs">When NPCI publishes a change, it will appear here automatically.</div>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400">
                <th className="px-5 py-3 font-medium">Change</th>
                <th className="px-5 py-3 font-medium">State</th>
                <th className="px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 font-medium">Docs</th>
                <th className="px-5 py-3 font-medium">Updated</th>
                <th className="px-5 py-3 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.change_id} className="border-b border-slate-50 last:border-0 hover:bg-slate-50">
                  <td className="px-5 py-3">
                    <Link to={`/changes/${encodeURIComponent(c.change_id)}`} className="font-medium text-brand-700 hover:underline">
                      {c.title}
                    </Link>
                    <div className="text-xs text-slate-400">
                      {c.change_id}
                      {c.blocked && <span className="ml-2 text-red-500">● blocked</span>}
                    </div>
                  </td>
                  <td className="px-5 py-3 text-slate-600">{c.state}</td>
                  <td className="px-5 py-3"><Badge value={c.bucket} /></td>
                  <td className="px-5 py-3 text-slate-600">{c.doc_count}</td>
                  <td className="px-5 py-3 text-slate-400">{c.updated_at?.slice(0, 16).replace("T", " ")}</td>
                  <td className="px-5 py-3 text-right">
                    <button
                      onClick={() => del(c)}
                      className="text-sm font-medium text-red-600 hover:underline"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
