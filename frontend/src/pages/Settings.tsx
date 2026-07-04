import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { Card, Button, Input, Textarea, Label, Banner } from "../components/ui";

interface Field {
  key: string;
  label: string;
  type: string;
  secret: boolean;
  required: boolean;
  multiline: boolean;
  placeholder: string;
  help: string;
  set: boolean;
  value: string;
}
interface Category {
  key: string;
  label: string;
  description: string;
  configured: boolean;
  fields: Field[];
}
interface SettingsResp {
  categories: Category[];
  required_configured: number;
  required_total: number;
}

export default function Settings() {
  const [data, setData] = useState<SettingsResp | null>(null);
  const [active, setActive] = useState("npci_connection");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  async function load() {
    const resp = await api.get<{ settings: SettingsResp }>("/admin/settings");
    setData(resp.settings);
  }
  useEffect(() => { load(); }, []);

  async function save() {
    setMsg(null);
    try {
      await api.put("/admin/settings", edits);
      setEdits({});
      await load();
      setMsg({ kind: "success", text: "Settings saved." });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Save failed." });
    }
  }

  async function testConnection() {
    setMsg(null);
    try {
      const r = await api.post<{ ok: boolean; error?: string }>("/admin/settings/test-connection");
      setMsg(r.ok ? { kind: "success", text: "Connection OK." } : { kind: "error", text: `Failed: ${r.error}` });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Test failed." });
    }
  }

  function onEdit(key: string, value: string) {
    setEdits((p) => ({ ...p, [key]: value }));
  }

  async function onUploadMd(field: Field, file: File | undefined) {
    if (!file) return;
    const text = await file.text();
    onEdit(field.key, text);
  }

  if (!data) return <div className="text-sm text-slate-500">Loading…</div>;

  const categories = data.categories ?? [];
  if (categories.length === 0) {
    return (
      <div>
        <h1 className="mb-1 text-2xl font-semibold text-slate-900">Settings</h1>
        <Banner kind="error">
          Settings couldn't be loaded in the expected format. If the app was just updated,
          restart the backend server so it serves the current settings schema.
        </Banner>
      </div>
    );
  }

  const cat = categories.find((c) => c.key === active) ?? categories[0];
  const dirty = Object.keys(edits).length > 0;

  return (
    <div>
      <h1 className="mb-1 text-2xl font-semibold text-slate-900">Settings</h1>
      <p className="mb-6 text-sm text-slate-500">
        {data.required_configured} of {data.required_total} required credentials configured.
      </p>

      {msg && <div className="mb-4"><Banner kind={msg.kind}>{msg.text}</Banner></div>}

      <div className="flex gap-6">
        <div className="w-60 shrink-0">
          {categories.map((c) => (
            <button
              key={c.key}
              onClick={() => setActive(c.key)}
              className={`mb-1 flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm ${
                active === c.key ? "bg-brand-50 text-brand-700" : "text-slate-600 hover:bg-slate-50"
              }`}
            >
              <span>{c.label}</span>
              <span
                className={`ml-2 h-2 w-2 shrink-0 rounded-full ${
                  c.configured ? "bg-emerald-500" : "bg-slate-300"
                }`}
                title={c.configured ? "Configured" : "Not set"}
              />
            </button>
          ))}
        </div>

        <Card className="flex-1 p-6">
          <div className="mb-1 flex items-center gap-2">
            <span className="text-base font-semibold text-slate-800">{cat.label}</span>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                cat.configured ? "bg-emerald-100 text-emerald-700" : "bg-slate-200 text-slate-500"
              }`}
            >
              {cat.configured ? "Configured" : "Not set"}
            </span>
          </div>
          {cat.description && <p className="mb-5 text-sm text-slate-500">{cat.description}</p>}

          <div className="space-y-5">
            {cat.fields.map((f) => (
              <div key={f.key}>
                <div className="flex items-center gap-2">
                  <Label>{f.label}</Label>
                  {f.secret && !f.set && (
                    <span className="mb-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-500">
                      Not set
                    </span>
                  )}
                </div>
                {f.multiline ? (
                  <>
                    <Textarea
                      rows={12}
                      className="font-mono text-xs"
                      defaultValue={f.value}
                      placeholder={f.placeholder}
                      onChange={(e) => onEdit(f.key, e.target.value)}
                    />
                    <div className="mt-2">
                      <input
                        ref={fileRef}
                        type="file"
                        accept=".md,text/markdown,text/plain"
                        className="hidden"
                        onChange={(e) => onUploadMd(f, e.target.files?.[0])}
                      />
                      <Button variant="ghost" onClick={() => fileRef.current?.click()}>
                        Upload .md
                      </Button>
                    </div>
                  </>
                ) : (
                  <Input
                    type={f.secret ? "password" : "text"}
                    defaultValue={f.secret ? "" : f.value}
                    placeholder={f.secret && f.set ? "••••••••" : f.placeholder}
                    onChange={(e) => onEdit(f.key, e.target.value)}
                  />
                )}
                {f.help && <p className="mt-1 text-xs text-slate-400">{f.help}</p>}
              </div>
            ))}
          </div>

          <div className="mt-6 flex gap-2">
            <Button onClick={save} disabled={!dirty}>Save Changes</Button>
            {active === "npci_connection" && (
              <Button variant="ghost" onClick={testConnection}>Test Connection</Button>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}
