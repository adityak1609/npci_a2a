// Thin fetch wrapper. Auth is cookie-based (HttpOnly, set by /auth/admin/login);
// same-origin requests send the cookie + an Origin header that satisfies the
// backend CSRF check, so no token handling is needed here.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type Json = Record<string, unknown>;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (res.status === 204) return undefined as T;

  let data: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    const detail =
      (data && typeof data === "object" && "detail" in data
        ? String((data as Json).detail)
        : typeof data === "string"
          ? data
          : res.statusText) || "Request failed";
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

// JSON-RPC call to the admin /a2a/internal endpoint.
export async function rpc<T>(method: string, params: Json): Promise<T> {
  const res = await api.post<{ result?: T; error?: { message: string } }>("/a2a/internal", {
    jsonrpc: "2.0",
    id: crypto.randomUUID(),
    method,
    params,
  });
  if (res && res.error) throw new ApiError(400, res.error.message);
  return (res?.result ?? {}) as T;
}
