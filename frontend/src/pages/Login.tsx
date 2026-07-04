import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Input, Label, Banner } from "../components/ui";

interface LoginResp {
  access_token?: string;
  mfa_required?: boolean;
  mfa_token?: string;
}

export default function Login() {
  const { refresh } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const resp = await api.post<LoginResp>("/auth/login/session", { email, password });
      if (resp.mfa_required && resp.mfa_token) {
        setMfaToken(resp.mfa_token);
      } else {
        await refresh();
        navigate("/");
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  async function verifyMfa(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.post("/auth/mfa/verify", { mfa_token: mfaToken, code });
      await refresh();
      navigate("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Verification failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-slate-50">
      <div className="w-full max-w-sm rounded-2xl border border-slate-200 bg-white p-8 shadow-sm">
        <div className="mb-6 flex items-center gap-2">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-600 text-base font-bold text-white">
            P
          </div>
          <div>
            <div className="font-semibold text-slate-800">Partner Platform</div>
            <div className="text-xs text-slate-400">Sign In</div>
          </div>
        </div>

        {error && <div className="mb-4"><Banner kind="error">{error}</Banner></div>}

        {!mfaToken ? (
          <form onSubmit={submit} className="space-y-4">
            <div>
              <Label>Username or Email</Label>
              <Input value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required />
            </div>
            <div>
              <Label>Password</Label>
              <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Signing in…" : "Sign In"}
            </Button>
          </form>
        ) : (
          <form onSubmit={verifyMfa} className="space-y-4">
            <div>
              <Label>Authenticator Code</Label>
              <Input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoFocus required />
            </div>
            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Verifying…" : "Verify"}
            </Button>
          </form>
        )}
      </div>
    </div>
  );
}
