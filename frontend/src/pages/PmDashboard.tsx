import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { StatCard, Card, Badge } from "../components/ui";

interface UpdateItem {
  kind: "counter_proposal" | "blocker" | "milestone" | "escalation" | "document" | "change_published";
  change_id: string;
  change_title: string;
  ref: string;
  status: string | null;
  npci_responded: boolean;
  detail: string;
  detail_full?: string | null;
  raised_by?: string | null;
  at: string | null;
  download_id?: number | null;
  hidden?: boolean;
}
interface OwnedChange {
  change_id: string;
  title: string;
  state: string;
  bucket: string;
  blocked: boolean;
  updated_at: string;
}
interface DashboardResp {
  is_admin: boolean;
  counts: { changes: number; updates: number; npci_responses: number; common_updates: number };
  changes: OwnedChange[];
  updates: UpdateItem[];
  common_updates: UpdateItem[];
}

const KIND_LABEL: Record<UpdateItem["kind"], string> = {
  counter_proposal: "Counter-proposal",
  blocker: "Blocker",
  milestone: "Milestone",
  escalation: "Escalation",
  document: "Document",
  change_published: "New change",
};

/** Timestamps arrive in UTC; display in IST. */
function fmtIst(ts: string | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts.endsWith("Z") || ts.includes("+") ? ts : ts + "Z");
  if (isNaN(d.getTime())) return ts.slice(0, 16).replace("T", " ");
  return d.toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).replace(",", "");
}

function UpdateList({
  items,
  emptyText,
  isAdmin,
  onHide,
  onUnhide,
}: {
  items: UpdateItem[];
  emptyText: string;
  isAdmin?: boolean;
  onHide?: (u: UpdateItem) => void;
  onUnhide?: (u: UpdateItem) => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  if (items.length === 0) {
    return (
      <div className="py-14 text-center text-slate-400">
        <div className="text-sm font-medium text-slate-500">Nothing yet</div>
        <div className="text-xs">{emptyText}</div>
      </div>
    );
  }
  return (
    <ul className="divide-y divide-slate-50">
      {items.map((u, i) => {
        const rowKey = `${u.kind}-${u.ref}-${i}`;
        const fullText = u.detail_full || u.detail;
        const expandable = !!fullText && (fullText !== u.detail || u.detail.length > 120);
        const isOpen = !!expanded[rowKey];
        return (
        <li
          key={rowKey}
          className={`flex items-start gap-3 px-5 py-3 ${u.hidden ? "opacity-50" : ""}`}
        >
          <span
            className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
              u.npci_responded ? "bg-emerald-500" : "bg-slate-300"
            }`}
            title={u.npci_responded ? "From NPCI" : "Awaiting NPCI"}
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium uppercase tracking-wide text-slate-400">
                {KIND_LABEL[u.kind]}
              </span>
              {u.status && <Badge value={u.status} />}
              {isAdmin && u.raised_by && (
                <span
                  className="rounded-full bg-brand-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-brand-700"
                  title="Portal user who raised this escalation"
                >
                  Raised by {u.raised_by}
                </span>
              )}
              {isAdmin && u.hidden && (
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-600">
                  Hidden from users
                </span>
              )}
            </div>
            <Link
              to={`/changes/${encodeURIComponent(u.change_id)}`}
              className="text-sm font-medium text-brand-700 hover:underline"
            >
              {u.change_title}
            </Link>
            {isOpen ? (
              <div className="mt-1 whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs leading-relaxed text-slate-700">
                {fullText}
              </div>
            ) : (
              <div
                className={`truncate text-xs text-slate-500 ${expandable ? "cursor-pointer hover:text-slate-700" : ""}`}
                onClick={() => expandable && setExpanded((e) => ({ ...e, [rowKey]: true }))}
                title={expandable ? "Click to read the full update" : undefined}
              >
                {u.detail}
              </div>
            )}
            {expandable && (
              <button
                onClick={() => setExpanded((e) => ({ ...e, [rowKey]: !isOpen }))}
                className="mt-0.5 text-xs font-medium text-brand-600 hover:underline"
              >
                {isOpen ? "Show less" : "Read more"}
              </button>
            )}
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <span className="text-xs text-slate-400">{fmtIst(u.at)}</span>
            {u.download_id != null && (
              <a
                href={`/documents/${u.download_id}/download`}
                download
                className="text-xs font-medium text-brand-600 hover:underline"
              >
                Download
              </a>
            )}
            {isAdmin && (
              u.hidden ? (
                <button
                  onClick={() => onUnhide?.(u)}
                  className="text-xs font-medium text-brand-600 hover:underline"
                >
                  Restore
                </button>
              ) : (
                <button
                  onClick={() => onHide?.(u)}
                  className="text-xs font-medium text-red-600 hover:underline"
                >
                  Delete
                </button>
              )
            )}
          </div>
        </li>
        );
      })}
    </ul>
  );
}

export default function PmDashboard() {
  const [data, setData] = useState<DashboardResp | null>(null);

  async function load() {
    setData(await api.get<DashboardResp>("/portal/dashboard"));
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 5000); // live auto-refresh
    return () => clearInterval(t);
  }, []);

  const isAdmin = !!data?.is_admin;
  const counts = data?.counts ?? { changes: 0, updates: 0, npci_responses: 0, common_updates: 0 };
  const updates = data?.updates ?? [];
  const commonUpdates = data?.common_updates ?? [];
  const changes = data?.changes ?? [];

  async function hide(u: UpdateItem) {
    await api.post("/admin/updates/hide", { kind: u.kind, ref: u.ref });
    await load();
  }
  async function unhide(u: UpdateItem) {
    await api.post("/admin/updates/unhide", { kind: u.kind, ref: u.ref });
    await load();
  }
  const adminProps = isAdmin ? { isAdmin, onHide: hide, onUnhide: unhide } : {};

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">
            {isAdmin ? "Updates" : "My Updates"}
          </h1>
          <p className="text-sm text-slate-500">
            {isAdmin
              ? "NPCI updates across assigned changes. Delete an update to remove it from users' dashboards."
              : "NPCI updates on the changes assigned to you. Only you see these."}
          </p>
        </div>
        <span className="flex items-center gap-1.5 text-xs text-emerald-600">
          <span className="h-2 w-2 rounded-full bg-emerald-500" /> Live · auto-refresh
        </span>
      </div>

      <div className="mb-6 grid grid-cols-4 gap-4">
        <StatCard label={isAdmin ? "Assigned Changes" : "My Changes"} value={counts.changes} />
        <StatCard label={isAdmin ? "Updates" : "My Updates"} value={counts.updates} />
        <StatCard label="NPCI Responses" value={counts.npci_responses} hint="replies from NPCI" />
        <StatCard label="Shared Updates" value={counts.common_updates} hint="common to all" />
      </div>

      <div className="mb-3 text-sm font-semibold text-slate-700">
        {isAdmin ? "Updates (assigned changes)" : "My updates"}
      </div>
      <Card className="mb-8">
        <UpdateList
          items={updates}
          emptyText="When NPCI responds on one of your changes, it appears here."
          {...adminProps}
        />
      </Card>

      <div className="mb-1 text-sm font-semibold text-slate-700">Shared updates</div>
      <p className="mb-3 text-xs text-slate-500">
        NPCI documents and newly published changes — visible to everyone, even after a change is assigned to a user.
      </p>
      <Card className="mb-8">
        <UpdateList
          items={commonUpdates}
          emptyText="New NPCI documents and unassigned changes show up here for everyone."
          {...adminProps}
        />
      </Card>

      <div className="mb-3 text-sm font-semibold text-slate-700">
        {isAdmin ? "Changes assigned to me" : "My changes"}
      </div>
      <Card>
        {changes.length === 0 ? (
          <div className="py-14 text-center text-slate-400">
            <div className="text-sm font-medium text-slate-500">No changes assigned to you yet</div>
            <div className="text-xs">
              Ask about a change in the Assistant, or an admin can assign one to you.
            </div>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400">
                <th className="px-5 py-3 font-medium">Change</th>
                <th className="px-5 py-3 font-medium">State</th>
                <th className="px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.change_id} className="border-b border-slate-50 last:border-0 hover:bg-slate-50">
                  <td className="px-5 py-3">
                    <Link
                      to={`/changes/${encodeURIComponent(c.change_id)}`}
                      className="font-medium text-brand-700 hover:underline"
                    >
                      {c.title}
                    </Link>
                    <div className="text-xs text-slate-400">
                      {c.change_id}
                      {c.blocked && <span className="ml-2 text-red-500">● blocked</span>}
                    </div>
                  </td>
                  <td className="px-5 py-3 text-slate-600">{c.state}</td>
                  <td className="px-5 py-3"><Badge value={c.bucket} /></td>
                  <td className="px-5 py-3 text-slate-400">{fmtIst(c.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
