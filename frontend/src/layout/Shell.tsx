import { NavLink, useNavigate } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "../lib/auth";

interface NavItem {
  to: string;
  label: string;
  end?: boolean;
  adminOnly?: boolean;
  userOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "Dashboard", end: true, adminOnly: true },
  { to: "/dashboard", label: "Updates", adminOnly: true },
  { to: "/dashboard", label: "My Updates", userOnly: true },
  { to: "/assistant", label: "Assistant" },
  { to: "/knowledge", label: "Knowledge Base" },
  { to: "/settings", label: "Settings", adminOnly: true },
  { to: "/users", label: "Users", adminOnly: true },
];

export default function Shell({ children }: { children: ReactNode }) {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const nav = NAV.filter((n) => {
    if (n.adminOnly) return me?.is_admin;
    if (n.userOnly) return !me?.is_admin;
    return true;
  });

  async function onLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <div className="flex h-full">
      <aside className="flex w-60 flex-col border-r border-slate-200 bg-white">
        <div className="flex items-center gap-2 px-5 py-5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-sm font-bold text-white">
            P
          </div>
          <span className="font-semibold text-slate-800">Partner Platform</span>
        </div>
        <nav className="flex-1 px-3">
          {nav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `mb-1 block rounded-lg px-3 py-2 text-sm font-medium ${
                  isActive ? "bg-brand-50 text-brand-700" : "text-slate-600 hover:bg-slate-50"
                }`
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-200 px-4 py-4 text-sm">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-200 text-xs font-semibold text-slate-600">
              {(me?.full_name || me?.email || "A").slice(0, 1).toUpperCase()}
            </div>
            <div className="min-w-0">
              <div className="truncate font-medium text-slate-800">{me?.full_name || me?.email}</div>
              <div className="text-xs text-slate-400">{me?.is_admin ? "Admin" : "User"}</div>
            </div>
          </div>
          <button onClick={onLogout} className="mt-3 text-xs text-slate-500 hover:text-slate-800">
            Logout
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <div className="mx-auto max-w-6xl px-8 py-8">{children}</div>
      </main>
    </div>
  );
}
