import { BarChart3, FlaskConical, Monitor, Settings as SettingsIcon, TrendingUp, Wrench } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { token } from "../services/api";
import { useTraderStore } from "../store/useTraderStore";
import { NotificationBell } from "./NotificationBell";
import { AlertBanner } from "./ui";

const Pill = ({ ok, warn, label, title }: { ok: boolean; warn?: boolean; label: string; title?: string }) => (
  <div title={title} className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-100 text-slate-700 text-xs font-medium border border-slate-200">
    <span className={`w-2 h-2 rounded-full ${ok ? "bg-green-500" : warn ? "bg-amber-500" : "bg-red-500"}`} />
    {label}
  </div>
);

const PHASE: Record<string, string> = { open: "Market Open", pre_open: "Pre-open", post_close: "After Close", closed: "Market Closed" };

export const Navbar = () => {
  const { snap, socket } = useTraderStore();
  const e = snap?.engine;
  const a = snap?.angel;
  const backendOk = socket === "connected";
  return (
    <nav className="fixed top-0 left-0 right-0 h-16 bg-white border-b border-slate-200 z-50 flex items-center justify-between px-6">
      <div>
        <h1 className="font-bold text-slate-900 text-lg">Indian Day Trader</h1>
        <p className="text-xs text-slate-500">ORB + EMA Pullback • Paper trading only</p>
      </div>
      <div className="flex items-center gap-2">
        <Pill ok={backendOk} label={backendOk ? "Backend connected" : "Backend offline"} />
        {snap && backendOk && (
          <>
            <Pill ok={!!e?.running} label={e?.running ? "Engine running" : "Engine stopped"} title={e?.last_error || ""} />
            <Pill ok={e?.phase === "open"} warn={e?.phase !== "open"} label={PHASE[e?.phase || ""] || "Market —"} title={e?.phase_reason || ""} />
            <Pill
              ok={!!a?.logged_in && !!a?.feed.connected}
              warn={!!a?.logged_in && !a?.feed.connected}
              label={a?.logged_in ? (a.feed.connected ? "Angel One live" : "Angel One: feed down") : "Angel One offline"}
              title={a?.last_error || a?.feed.last_error || ""}
            />
          </>
        )}
        <NotificationBell />
        {token.get() && (
          <button
            title="Forget the access token in this browser"
            onClick={() => {
              token.clear();
              window.location.reload();
            }}
            className="px-3 py-1.5 rounded-full text-xs font-medium text-slate-600 border border-slate-200 hover:bg-slate-100"
          >
            Lock
          </button>
        )}
      </div>
    </nav>
  );
};

const NAV = [
  { to: "/", label: "Dashboard", icon: BarChart3 },
  { to: "/signals", label: "Signals", icon: Monitor },
  { to: "/positions", label: "Trades", icon: TrendingUp },
  { to: "/backtest", label: "Backtest", icon: FlaskConical },
  { to: "/skills", label: "Skills", icon: Wrench },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

export const Layout = () => {
  const { snap, socket } = useTraderStore();
  return (
    <div className="min-h-screen bg-slate-50">
      <Navbar />
      <aside className="fixed left-0 top-16 bottom-0 w-56 bg-white border-r border-slate-200 p-4 space-y-1">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium ${
                isActive ? "bg-indian-green/10 text-indian-green border border-indian-green/20" : "text-slate-600 hover:bg-slate-100"
              }`
            }
          >
            <n.icon className="w-4 h-4" />
            {n.label}
          </NavLink>
        ))}
      </aside>
      <main className="ml-56 pt-20 pb-8 px-6 max-w-[1400px]">
        {socket !== "connected" && (
          <AlertBanner type="error" message={snap ? "Lost the backend connection: showing the last data received, reconnecting…" : "Connecting to the backend…"} />
        )}
        <Outlet />
      </main>
    </div>
  );
};
