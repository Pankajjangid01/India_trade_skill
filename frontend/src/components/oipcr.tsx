import type { OiPcr } from "../store/useTraderStore";
import { Badge, num } from "./ui";

const pct = (x: number | null | undefined) => (x == null ? "—" : `${x > 0 ? "+" : ""}${x.toFixed(1)}%`);
const lakh = (x: number) => `${(x / 1e5).toFixed(1)} L`;

// one OI shift since the battleground: `good` is the direction that supports the trade at that wall
const Shift = ({ label, v, good }: { label: string; v: number | null | undefined; good: "up" | "down" }) => {
  const tone = v == null || v === 0 ? "text-slate-500" : (v > 0) === (good === "up") ? "text-green-700" : "text-red-700";
  return (
    <div>
      <p className="text-slate-500">{label}</p>
      <p className={`font-medium ${tone}`}>{pct(v)}</p>
    </div>
  );
};

export const OiPcrPanel = ({ market }: { market: { symbol: string; ltp: number | null; oi_pcr?: OiPcr | null }[] }) => {
  const rows = market.filter((m) => m.oi_pcr);
  if (rows.length === 0)
    return <p className="text-sm text-slate-500">The battleground is marked at 09:30 once the engine has evaluated the first bars.</p>;
  if (!rows.some((m) => m.oi_pcr!.enabled))
    return <p className="text-sm text-slate-500">OI Shift + PCR is off: turn on OI_PCR in Settings (paper trades only).</p>;
  const traded = rows.filter((m) => m.oi_pcr!.traded);
  if (traded.length === 0)
    return <p className="text-sm text-slate-500">OI_PCR trades {rows[0].oi_pcr!.symbols.join(", ") || "no index"}: not in today's indices.</p>;
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {traded.map(({ symbol, ltp, oi_pcr: o }) => {
        const b = o!.baseline, n = o!.now;
        return (
          <div key={symbol} className="rounded-lg border border-slate-200 p-4">
            <div className="flex justify-between items-center">
              <span className="font-semibold">{symbol}</span>
              <span className="text-xs text-slate-500">{o!.snapshots ?? 0} chain records today</span>
            </div>
            {!b ? (
              <p className="text-xs text-slate-500 mt-2">{o!.note || "No battleground yet."}</p>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-2 text-xs mt-3">
                  <div className="rounded bg-red-50 border border-red-100 p-2">
                    <p className="text-slate-500">Call wall (resistance)</p>
                    <p className="font-semibold">{num(b.call_wall, 0)}</p>
                    <p className="text-[11px] text-slate-500">{lakh(b.call_wall_oi)} call OI at 09:30</p>
                  </div>
                  <div className="rounded bg-green-50 border border-green-100 p-2">
                    <p className="text-slate-500">Put wall (support)</p>
                    <p className="font-semibold">{num(b.put_wall, 0)}</p>
                    <p className="text-[11px] text-slate-500">{lakh(b.put_wall_oi)} put OI at 09:30</p>
                  </div>
                </div>
                <div className="flex justify-between text-xs mt-2">
                  <span>
                    PCR <span className="font-medium">{b.pcr?.toFixed(2) ?? "—"}</span> at 09:30
                    {b.opening && <Badge tone="slate">{b.opening}</Badge>}
                  </span>
                  <span>
                    now <span className="font-medium">{n?.pcr?.toFixed(2) ?? "—"}</span>
                    {n?.pcr_prev != null && <span className="text-slate-400"> (prev {n.pcr_prev.toFixed(2)})</span>}
                  </span>
                </div>
                {n ? (
                  <div className="grid grid-cols-4 gap-2 text-xs mt-3" aria-label="OI change since 09:30 at the walls">
                    <Shift label={`${num(b.call_wall, 0)} call`} v={n.call_wall_ce_pct} good="down" />
                    <Shift label={`${num(b.call_wall, 0)} put`} v={n.call_wall_pe_pct} good="up" />
                    <Shift label={`${num(b.put_wall, 0)} put`} v={n.put_wall_pe_pct} good="down" />
                    <Shift label={`${num(b.put_wall, 0)} call`} v={n.put_wall_ce_pct} good="up" />
                  </div>
                ) : (
                  <p className="text-xs text-slate-400 mt-3">OI changes appear from the 09:30 bar on.</p>
                )}
                <p className="text-[11px] text-slate-400 mt-1">
                  OI change since 09:30. Green: writers leaving a wall or stepping in behind a breakout. Spot {num(ltp ?? n?.spot ?? b.spot)}
                </p>
              </>
            )}
            <div className="mt-3">
              <p className="text-xs font-medium text-slate-600 mb-1">At a wall today</p>
              {(o!.decisions || []).length === 0 ? (
                <p className="text-xs text-slate-400">Price has not reached a wall: no trade is forced.</p>
              ) : (
                <ul className="space-y-1">
                  {o!.decisions!.slice().reverse().map((d) => (
                    <li key={d.bar_start_ist + d.direction} className="text-xs">
                      <span className={d.accepted ? "text-green-700 font-medium" : "text-slate-500"}>
                        {d.bar_close_ist} {d.direction === "bullish" ? "▲" : "▼"} {d.accepted ? "accepted" : "rejected"}
                      </span>
                      <span className="text-slate-400"> — {d.reason.replace(/^accepted: /, "")}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
};
