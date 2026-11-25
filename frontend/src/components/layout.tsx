import { User, Monitor, BarChart3, Settings as SettingsIcon, Bell, AlertCircle, CheckCircle2, TrendingUp, TrendingDown } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { NotificationBell } from "./NotificationBell";

export const Navbar = () => (
  <nav className="fixed top-0 left-0 right-0 h-16 bg-white border-b border-slate-200 z-50 flex items-center justify-between px-6">
    <div className="flex items-center gap-3">
      <div className="w-8 h-8 bg-indian-green rounded-lg flex items-center justify-center">
        <User className="w-5 h-5 text-white" />
      </div>
      <div>
        <h1 className="font-bold text-slate-900 text-lg">Indian Day Trader</h1>
        <p className="text-xs text-slate-500">Live ORB Signal & Auto Paper Trading</p>
      </div>
    </div>

    <div className="flex items-center gap-4">
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-100 text-slate-600 text-sm font-medium border border-slate-200">
        <span className="w-2 h-2 rounded-full bg-slate-400 animate-pulse" />
        <span id="market-status">Market Closed</span>
      </div>
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-900 text-white text-sm font-medium">
        <span className="w-2 h-2 rounded-full bg-indian-green animate-pulse" />
        <span id="system-status">System Live</span>
      </div>
      <Bell className="w-5 h-5 text-slate-500 hover:text-slate-700 cursor-pointer" />
    </div>
  </nav>
);

export const Sidebar = ({ active }: { active: string }) => {
  const navigate = useNavigate();
  const handleNav = (key: string) => {
    if (key === "dashboard") navigate("/");
    if (key === "signals") navigate("/signals");
    if (key === "positions") navigate("/positions");
    if (key === "settings") navigate("/settings");
  };

  return (
    <aside className="fixed left-0 top-16 bottom-0 w-64 bg-white border-r border-slate-200 z-40 flex flex-col">
      <div className="p-4">
        <nav className="space-y-1">
          {[
            { key: "dashboard", label: "Dashboard", icon: BarChart3 },
            { key: "signals", label: "Live Signals", icon: Monitor },
            { key: "positions", label: "Positions", icon: TrendingUp },
            { key: "settings", label: "Settings", icon: SettingsIcon },
          ].map((item) => (
            <button
              key={item.key}
              onClick={() => handleNav(item.key)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all ${
                active === item.key
                  ? "bg-indian-green/10 text-indian-green border border-indian-green/20"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              <item.icon className="w-4 h-4" />
              {item.label}
            </button>
          ))}
        </nav>
      </div>
    </aside>
  );
};

export const AlertBanner = ({ message, type = "info" }: { message: string; type?: "info" | "success" | "warning" | "error" }) => {
  const variants: Record<string, string> = {
    info: "bg-blue-50 border-blue-200 text-blue-800",
    success: "bg-green-50 border-green-200 text-green-800",
    warning: "bg-amber-50 border-amber-200 text-amber-800",
    error: "bg-red-50 border-red-200 text-red-800",
  };
  return (
    <div className={`border rounded-lg p-3 flex items-center gap-3 mb-4 ${variants[type]}`}>
      <AlertCircle className="w-4 h-4" />
      <span className="text-sm">{message}</span>
    </div>
  );
};

export const StatCard = ({ title, value, sub, trend }: { title: string; value: string | number; sub: string; trend?: "up" | "down" | "neutral" }) => (
  <div className="bg-white rounded-xl border border-slate-200 p-5">
    <p className="text-xs text-slate-500 font-medium">{title}</p>
    <div className="flex items-baseline gap-1 mt-1">
      <span className="text-2xl font-bold text-slate-900">{value}</span>
      {trend === "up" && <TrendingUp className="w-4 h-4 text-green-500" />}
      {trend === "down" && <TrendingDown className="w-4 h-4 text-red-500" />}
    </div>
    <p className="text-xs text-slate-400 mt-1">{sub}</p>
  </div>
);
