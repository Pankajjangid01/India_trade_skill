import { useState } from "react";
import { Bell, CheckCircle2, Clock, Zap } from "lucide-react";
import { useTraderStore } from "../store/useTraderStore";

const icons = {
  signal: <Zap className="w-4 h-4 text-amber-500" />,
  fill: <CheckCircle2 className="w-4 h-4 text-green-600" />,
  exit: <Clock className="w-4 h-4 text-slate-500" />,
  system: <Bell className="w-4 h-4 text-slate-500" />,
};

export const NotificationBell = () => {
  const { notifications, markAllRead } = useTraderStore();
  const [open, setOpen] = useState(false);
  const unread = notifications.filter((n) => !n.read).length;
  return (
    <div className="relative">
      <button
        aria-label="Notifications"
        onClick={() => {
          setOpen(!open);
          if (open) markAllRead();
        }}
        className="relative w-9 h-9 rounded-full bg-white border border-slate-200 flex items-center justify-center hover:bg-slate-50"
      >
        <Bell className="w-5 h-5 text-slate-500" />
        {unread > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 bg-red-500 text-white text-[10px] rounded-full flex items-center justify-center">
            {unread}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 top-11 w-96 bg-white border border-slate-200 rounded-xl shadow-lg z-50 max-h-96 overflow-y-auto">
          <p className="p-3 border-b text-xs font-bold text-slate-900 uppercase">Events this session</p>
          {notifications.length === 0 && <p className="p-4 text-sm text-slate-500">Nothing yet.</p>}
          {notifications.map((n) => (
            <div key={n.id} className={`flex gap-3 p-3 border-b border-slate-50 ${n.read ? "" : "bg-amber-50/40"}`}>
              {icons[n.type]}
              <div className="flex-1">
                <p className="text-sm font-semibold text-slate-900">{n.title}</p>
                <p className="text-xs text-slate-500">{n.body}</p>
                <p className="text-[10px] text-slate-400 mt-1">{new Date(n.at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })}</p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
