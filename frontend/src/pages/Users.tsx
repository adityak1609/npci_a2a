import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { StatCard, Card, Button, Input, Label, Badge, Banner } from "../components/ui";

interface UserRow { id: number; username: string; full_name: string | null; role: string; status: string }
interface UsersResp { counts: { total: number; active: number; inactive: number; admins: number }; users: UserRow[] }

export default function Users() {
  const { me } = useAuth();
  const [data, setData] = useState<UsersResp | null>(null);
  const [form, setForm] = useState({ username: "", password: "", full_name: "", role: "user" });
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  async function load() {
    setData(await api.get<UsersResp>("/admin/users"));
  }
  useEffect(() => { load(); }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    try {
      await api.post("/admin/users", form);
      setForm({ username: "", password: "", full_name: "", role: "user" });
      await load();
      setMsg({ kind: "success", text: "User created." });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Create failed." });
    }
  }

  async function toggle(u: UserRow) {
    const verb = u.status === "active" ? "deactivate" : "activate";
    try {
      await api.post(`/admin/users/${u.id}/${verb}`);
      await load();
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Action failed." });
    }
  }

  async function remove(u: UserRow) {
    if (!window.confirm(
      `Delete ${u.role} "${u.username}"?\n\nThis anonymizes their data, removes their sessions, and blocks login. This cannot be undone.`
    )) return;
    setMsg(null);
    try {
      await api.del(`/admin/users/${u.id}`);
      await load();
      setMsg({ kind: "success", text: `Deleted ${u.username}.` });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Delete failed." });
    }
  }

  const counts = data?.counts ?? { total: 0, active: 0, inactive: 0, admins: 0 };

  return (
    <div>
      <h1 className="mb-1 text-2xl font-semibold text-slate-900">User Management</h1>
      <p className="mb-6 text-sm text-slate-500">Portal accounts for this partner node.</p>

      <div className="mb-6 grid grid-cols-4 gap-4">
        <StatCard label="Total Users" value={counts.total} />
        <StatCard label="Active" value={counts.active} />
        <StatCard label="Inactive" value={counts.inactive} />
        <StatCard label="Admins" value={counts.admins} hint="full access" />
      </div>

      {msg && <div className="mb-4"><Banner kind={msg.kind}>{msg.text}</Banner></div>}

      <Card className="mb-6 p-6">
        <form onSubmit={create} className="grid grid-cols-2 gap-4">
          <div><Label>Username</Label><Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required /></div>
          <div><Label>Password</Label><Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required /></div>
          <div><Label>Full Name</Label><Input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></div>
          <div>
            <Label>Role</Label>
            <select
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600"
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value })}
            >
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </div>
          <div><Button type="submit">Create User</Button></div>
        </form>
      </Card>

      <Card>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400">
              <th className="px-5 py-3 font-medium">Username</th>
              <th className="px-5 py-3 font-medium">Full Name</th>
              <th className="px-5 py-3 font-medium">Role</th>
              <th className="px-5 py-3 font-medium">Status</th>
              <th className="px-5 py-3 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(data?.users ?? []).map((u) => (
              <tr key={u.id} className="border-b border-slate-50 last:border-0">
                <td className="px-5 py-3 text-slate-700">{u.username}</td>
                <td className="px-5 py-3 text-slate-600">{u.full_name || "—"}</td>
                <td className="px-5 py-3"><Badge value={u.role} /></td>
                <td className="px-5 py-3"><Badge value={u.status} /></td>
                <td className="px-5 py-3">
                  <div className="flex items-center gap-3">
                    <button onClick={() => toggle(u)} className="text-sm text-brand-700 hover:underline">
                      {u.status === "active" ? "Deactivate" : "Activate"}
                    </button>
                    {me?.id !== u.id && (
                      <button onClick={() => remove(u)} className="text-sm text-red-600 hover:underline">
                        Delete
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
