import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./lib/auth";
import Shell from "./layout/Shell";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import PmDashboard from "./pages/PmDashboard";
import ChangeDetail from "./pages/ChangeDetail";
import Settings from "./pages/Settings";
import Users from "./pages/Users";
import KnowledgeBase from "./pages/KnowledgeBase";
import Assistant from "./pages/Assistant";

function Protected({ children, adminOnly }: { children: JSX.Element; adminOnly?: boolean }) {
  const { me, loading } = useAuth();
  if (loading) {
    return <div className="flex h-full items-center justify-center text-slate-400">Loading…</div>;
  }
  if (!me) return <Navigate to="/login" replace />;
  // Non-admin bank product managers land on their own dashboard; admin-only
  // surfaces (Incoming Changes board, Settings, Users) are not available to them.
  if (adminOnly && !me.is_admin) return <Navigate to="/dashboard" replace />;
  return <Shell>{children}</Shell>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Protected adminOnly><Dashboard /></Protected>} />
      <Route path="/dashboard" element={<Protected><PmDashboard /></Protected>} />
      <Route path="/changes/:changeId" element={<Protected><ChangeDetail /></Protected>} />
      <Route path="/settings" element={<Protected adminOnly><Settings /></Protected>} />
      <Route path="/users" element={<Protected adminOnly><Users /></Protected>} />
      <Route path="/knowledge" element={<Protected><KnowledgeBase /></Protected>} />
      <Route path="/assistant" element={<Protected><Assistant /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
