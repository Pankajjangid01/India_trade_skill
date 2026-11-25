import { Bell, X, Clock, CheckCircle2, AlertTriangle } from "lucide-react";

const icons = {
  signal: <Bell className="w-4 h-4 text-indian-green" />,
  alert: <AlertTriangle className="w-4 h-4 text-amber-500" />,
  fill: <CheckCircle2 className="w-4 h-4 text-blue-500" />,
  exit: <Clock className="w-4 h-4 text-slate-500" />,
  system: <AlertTriangle className="w-4 h-4 text-slate-500" />,
};

export const NotificationBell = ({
  notifications,
  onRemove,
}: {
  notifications: any[];
  onRemove: (id: string) => void;
}) => {
  const unread = notifications.filter((n) => !n.read);
  return (
    <div className="relative">
      <button className="relative w-8 h-8 rounded-full bg-white border border-slate-200 flex items-center justify-center hover:bg-slate-50 transition">
        <Bell className="w-5 h-5 text-slate-500" />
        {unread.length > 0 && (
          <span className="absolute top-1 right-1 w-2.5 h-2.5 bg-red-500 rounded-full border-2 border-white"></span>
        )}
      </button>
      {unread.length > 0 && (
        <div className="absolute right-0 top-10 w-80 bg-white border border-slate-200 rounded-xl shadow-lg z-50">
          <div className="p-3 border-b border-slate-100">
            <p className="text-xs font-bold text-slate-900 uppercase tracking-wider">Notifications</p>
            <p className="text-xs text-slate-500">{unread.length} new</p>
          </div>
          <div className="max-h-60 overflow-y-auto py-2">
            {unread.map((n) => (
              <div
                key={n.id}
                onClick={() => onRemove(n.id)}
                className="flex items-start gap-3 p-3 hover:bg-slate-50 cursor-pointer border-b border-slate-50 last:border-0"
              >
                {icons[n.type as keyof typeof icons] || icons.system}
                <div className="flex-1">
                  <p className="text-sm font-semibold text-slate-900">{n.title}</p>
                  <p className="text-xs text-slate-500">{n.body}</p>
                  <p className="text-[10px] text-slate-400 mt-1">{new Date(n.timestamp).toLocaleTimeString("en-IN")}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
