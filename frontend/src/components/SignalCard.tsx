import { ArrowUpRight, ArrowDownRight, Minus } from "lucide-react";

export const SignalCard = ({ signal }: { signal: any }) => {
  const dir = signal.direction || "wait";
  const isBullish = dir === "bullish";
  const isBearish = dir === "bearish";
  const statusColor =
    signal.status === "triggered"
      ? "bg-green-100 text-green-800 border-green-200"
      : signal.status === "waiting"
        ? "bg-amber-100 text-amber-800 border-amber-200"
        : signal.status === "expired"
          ? "bg-red-100 text-red-800 border-red-200"
          : "bg-slate-100 text-slate-600 border-slate-200";

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-indian-green" />
          <span className="font-semibold text-slate-900">{signal.symbol}</span>
        </div>
        <span className={`px-2 py-1 rounded text-xs font-medium border ${statusColor}`}>
          {signal.status}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <div>
          <p className="text-xs text-slate-500">Direction</p>
          <p className="font-medium text-slate-900 capitalize">
            {dir === "bullish" ? "Bullish (CE)" : dir === "bearish" ? "Bearish (PE)" : "Waiting"}
          </p>
        </div>
        <div>
          <p className="text-xs text-slate-500">Trigger Level</p>
          <p className="font-medium text-slate-900">{signal.trigger_level?.toLocaleString("en-IN") || "—"}</p>
        </div>
      </div>

      {signal.bar_close && (
        <div className="mb-4">
          <p className="text-xs text-slate-500">Bar Close</p>
          <p className="font-medium text-slate-900">{signal.bar_close.toLocaleString("en-IN")}</p>
          <p className="text-xs text-slate-400 mt-1">{signal.bar_close_ist}</p>
        </div>
      )}

      {signal.pick && (
        <div className="bg-slate-50 rounded-lg p-3 mb-4">
          <p className="text-xs text-slate-500 uppercase font-semibold mb-1">Pick (in-chain)</p>
          <p className="text-sm font-medium text-slate-900">{signal.pick.instrument?.tradingsymbol || "—"}</p>
          <p className="text-xs text-slate-500">Premium: {signal.pick.premium?.toLocaleString("en-IN")} | Delta: {signal.pick.delta}</p>
        </div>
      )}

      {signal.plan && (
        <div className="bg-slate-50 rounded-lg p-3">
          <p className="text-xs text-slate-500 uppercase font-semibold mb-1">Plan (in-exit)</p>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <p className="text-[10px] text-slate-400">Premium Stop</p>
              <p className="text-xs font-medium text-slate-900">{signal.plan.premium_stop?.toLocaleString("en-IN")}</p>
            </div>
            <div>
              <p className="text-[10px] text-slate-400">Premium Target</p>
              <p className="text-xs font-medium text-slate-900">{signal.plan.target_premium?.toLocaleString("en-IN")}</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export const PositionCard = ({ pos }: { pos: any }) => {
  const pnlColor = pos.pnl >= 0 ? "text-green-600" : "text-red-600";
  const pnlIcon = pos.pnl >= 0 ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />;
  const isActive = pos.status === "PAPER_ACTIVE";

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-slate-900">{pos.symbol}</span>
        <span className={`px-2 py-0.5 rounded text-xs font-medium ${isActive ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-600"}`}>
          {pos.status}
        </span>
      </div>
      <div className="flex items-center justify-between mb-2">
        <div>
          <p className="text-[10px] text-slate-400 uppercase">Entry</p>
          <p className="text-sm font-medium text-slate-900">{pos.entry_price?.toLocaleString("en-IN")}</p>
        </div>
        <div>
          <p className="text-[10px] text-slate-400 uppercase">SL</p>
          <p className="text-sm font-medium text-slate-900">{pos.stop_loss?.toLocaleString("en-IN")}</p>
        </div>
        <div>
          <p className="text-[10px] text-slate-400 uppercase">Target</p>
          <p className="text-sm font-medium text-slate-900">{pos.target?.toLocaleString("en-IN")}</p>
        </div>
      </div>
      <div className="flex items-center justify-between pt-2 border-t border-slate-50">
        <div>
          <p className="text-[10px] text-slate-400 uppercase">P&L</p>
          <p className={`text-sm font-bold ${pnlColor}`}>
            {pnlIcon} {pos.pnl >= 0 ? "+" : ""}{pos.pnl.toLocaleString("en-IN")}
          </p>
        </div>
        {isActive && (
          <button
            onClick={() => {
              // Manual close handled via API
            }}
            className="text-xs text-red-500 hover:text-red-700 underline"
          >
            Close Position
          </button>
        )}
      </div>
    </div>
  );
};
