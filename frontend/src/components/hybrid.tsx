import type { Hybrid } from "../store/useTraderStore";
import { Badge, num } from "./ui";

// the opening range as a band, the live price as a marker: inside, above the high or below the low
const RangeBar = ({ h, ltp }: { h: Hybrid; ltp: number | null }) => {
  const r = h.opening_range!;
  const span = Math.max(r.high - r.low, 1);
  const lo = r.low - span, hi = r.high + span;
  const pos = (v: number) => `${Math.min(100, Math.max(0, ((v - lo) / (hi - lo)) * 100))}%`;
  return (
    <div className="mt-2">
      <div className="relative h-6 rounded bg-slate-100" aria-label="Opening range and live price">
        <div className="absolute top-0 bottom-0 bg-violet-200 border-x border-violet-500"
          style={{ left: pos(r.low), width: `calc(${pos(r.high)} - ${pos(r.low)})` }} />
        {ltp != null && (
          <div className="absolute -top-1 -bottom-1 w-0.5 bg-slate-900" style={{ left: pos(ltp) }} title={`LTP ${num(ltp)}`} />
        )}
      </div>
      <div className="flex justify-between text-[11px] text-slate-500 mt-1">
        <span>low {num(r.low)}</span>
        <span>{r.pct.toFixed(2)}% wide (min {h.min_or_pct.toFixed(2)}%)</span>
        <span>high {num(r.high)}</span>
      </div>
    </div>
  );
};

export const HybridPanel = ({ market }: { market: { symbol: string; ltp: number | null; hybrid?: Hybrid | null }[] }) => {
  const rows = market.filter((m) => m.hybrid);
  if (rows.length === 0)
    return <p className="text-sm text-slate-500">The opening range appears after 09:30 once the engine has evaluated the first bars.</p>;
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {rows.map(({ symbol, ltp, hybrid: h }) => (
        <div key={symbol} className="rounded-lg border border-slate-200 p-4">
          <div className="flex justify-between items-center">
            <span className="font-semibold">{symbol}</span>
            <Badge tone={h!.enabled ? "green" : "slate"}>{h!.enabled ? "hybrid on" : "hybrid off (Settings)"}</Badge>
          </div>
          {h!.opening_range ? <RangeBar h={h!} ltp={ltp} /> : <p className="text-xs text-slate-500 mt-2">No opening range yet.</p>}
          <div className="grid grid-cols-3 gap-2 text-xs mt-3">
            <div>
              <p className="text-slate-500">Range</p>
              <Badge tone={h!.range_ok ? "green" : "amber"}>{h!.range_ok ? "wide enough" : h!.opening_range?.complete ? "too narrow" : "forming"}</Badge>
            </div>
            <div>
              <p className="text-slate-500">Breakout</p>
              <Badge tone={h!.breakout?.startsWith("inside") ? "slate" : "blue"}>{h!.breakout || "—"}</Badge>
            </div>
            <div>
              <p className="text-slate-500">EMA {h!.ema ? h!.ema.period.join("/") : ""}</p>
              <Badge tone={h!.ema?.trend === "bullish" ? "green" : h!.ema?.trend === "bearish" ? "red" : "slate"}>
                {h!.ema ? h!.ema.trend : h!.prev_day ? "warming up" : "needs yesterday"}
              </Badge>
            </div>
          </div>
          {h!.ema && <p className="text-[11px] text-slate-400 mt-1">fast {num(h!.ema.fast)} · slow {num(h!.ema.slow)}</p>}
          <div className="mt-3">
            <p className="text-xs font-medium text-slate-600 mb-1">EMA pullback setups today</p>
            {h!.setups.length === 0 ? (
              <p className="text-xs text-slate-400">None yet: no trade is forced without a setup.</p>
            ) : (
              <ul className="space-y-1">
                {h!.setups.slice().reverse().map((s) => (
                  <li key={s.bar_start_ist + s.direction} className="text-xs">
                    <span className={s.accepted ? "text-green-700 font-medium" : "text-slate-500"}>
                      {s.bar_close_ist} {s.direction === "bullish" ? "▲" : "▼"} {s.accepted ? "accepted" : "rejected"}
                    </span>
                    <span className="text-slate-400"> — {s.reason.replace(/^accepted: /, "")}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ))}
    </div>
  );
};
