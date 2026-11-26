import { AlertCircle } from "lucide-react";
import type { ReactNode } from "react";

const IST = "Asia/Kolkata";

export const inr = (n: number | null | undefined, sign = false) =>
  n == null
    ? "—"
    : `${sign && n > 0 ? "+" : ""}₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const num = (n: number | null | undefined, d = 2) =>
  n == null ? "—" : n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

export const timeIST = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString("en-IN", { timeZone: IST, hour12: false }) : "—";

export const pnlClass = (n: number | null | undefined) =>
  n == null ? "text-slate-500" : n > 0 ? "text-green-600" : n < 0 ? "text-red-600" : "text-slate-700";

export const todayIST = () => new Date().toLocaleDateString("en-CA", { timeZone: IST });

const tones: Record<string, string> = {
  green: "bg-green-100 text-green-800 border-green-200",
  red: "bg-red-100 text-red-800 border-red-200",
  amber: "bg-amber-100 text-amber-800 border-amber-200",
  blue: "bg-blue-100 text-blue-800 border-blue-200",
  slate: "bg-slate-100 text-slate-700 border-slate-200",
  violet: "bg-violet-100 text-violet-800 border-violet-200",
};

export const Badge = ({ tone = "slate", children, title }: { tone?: string; children: ReactNode; title?: string }) => (
  <span title={title} className={`inline-block px-2 py-0.5 rounded border text-xs font-medium whitespace-nowrap ${tones[tone]}`}>
    {children}
  </span>
);

export const STATUS_TONE: Record<string, string> = {
  EXECUTED: "green",
  EXECUTABLE: "blue",
  ENTRY_CUTOFF: "amber",
  REJECTED: "red",
  OPEN: "blue",
  CLOSED: "slate",
};
export const STATUS_LABEL: Record<string, string> = {
  EXECUTED: "Executed",
  EXECUTABLE: "Executable",
  ENTRY_CUTOFF: "Entry Cutoff",
  REJECTED: "Rejected",
};

export const StrategyBadge = ({ k }: { k: string }) => (
  <Badge tone={k === "ORB" ? "violet" : "blue"}>{k === "ORB" ? "ORB" : k === "EMA_PULLBACK" ? "EMA Pullback" : k}</Badge>
);

export const Direction = ({ d }: { d: string }) => (
  <span className={d === "bullish" ? "text-green-700 font-medium" : "text-red-700 font-medium"}>
    {d === "bullish" ? "▲ Bullish (CE)" : "▼ Bearish (PE)"}
  </span>
);

export const AlertBanner = ({ message, type = "info" }: { message: ReactNode; type?: "info" | "warning" | "error" }) => {
  const v = { info: "bg-blue-50 border-blue-200 text-blue-800", warning: "bg-amber-50 border-amber-200 text-amber-800", error: "bg-red-50 border-red-200 text-red-800" };
  return (
    <div className={`border rounded-lg p-3 flex items-center gap-3 mb-4 text-sm ${v[type]}`}>
      <AlertCircle className="w-4 h-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
};

export const StatCard = ({ title, value, sub, valueClass = "" }: { title: string; value: ReactNode; sub?: ReactNode; valueClass?: string }) => (
  <div className="bg-white rounded-xl border border-slate-200 p-4">
    <p className="text-xs text-slate-500 font-medium">{title}</p>
    <p className={`text-xl font-bold mt-1 text-slate-900 ${valueClass}`}>{value}</p>
    {sub != null && <p className="text-xs text-slate-400 mt-1">{sub}</p>}
  </div>
);

export const Card = ({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) => (
  <div className="bg-white rounded-xl border border-slate-200 mb-6">
    <div className="flex items-center justify-between px-5 py-3 border-b border-slate-100">
      <h2 className="font-bold text-slate-900">{title}</h2>
      {right}
    </div>
    <div className="p-5">{children}</div>
  </div>
);

export const Empty = ({ children }: { children: ReactNode }) => <p className="text-sm text-slate-500 text-center py-6">{children}</p>;
