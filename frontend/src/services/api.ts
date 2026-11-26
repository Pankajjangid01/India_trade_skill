// REST calls to the backend. Errors carry the backend's own message.
// VITE_API_URL is the deployed backend (https://<service>.onrender.com); empty means same origin (the Vite proxy).
import { useTraderStore } from "../store/useTraderStore";

export const BACKEND = (import.meta.env.VITE_API_URL || "").replace(/\/+$/, "");

// The backend's APP_TOKEN, typed in once and kept in this browser only (never built into the app).
const KEY = "idt_access_token";
export const token = {
  get: () => localStorage.getItem(KEY) || "",
  set: (t: string) => localStorage.setItem(KEY, t.trim()),
  clear: () => localStorage.removeItem(KEY),
};

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body) headers["Content-Type"] = "application/json";
  if (token.get()) headers.Authorization = `Bearer ${token.get()}`;
  let res: Response;
  try {
    res = await fetch(`${BACKEND}/api/v1${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new Error("Backend unreachable: is it running?");
  }
  if (res.status === 401) {
    useTraderStore.getState().setLocked(true);
    throw new Error("Access token missing or wrong");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x: any) => x.msg).join("; ") : res.statusText;
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return data as T;
}

export const api = {
  state: () => call<any>("GET", "/state"),
  start: () => call<any>("POST", "/engine/start"),
  stop: () => call<any>("POST", "/engine/stop"),
  closeTrade: (id: string) => call<any>("POST", `/trades/${encodeURIComponent(id)}/close`),
  trades: (date: string) => call<any[]>("GET", `/trades?date=${date}`),
  signals: (date: string) => call<any[]>("GET", `/signals?date=${date}`),
  daily: (days = 30) => call<any[]>("GET", `/stats/daily?days=${days}`),
  settings: () => call<any>("GET", "/settings"),
  saveSettings: (s: unknown) => call<any>("POST", "/settings", s),
};
