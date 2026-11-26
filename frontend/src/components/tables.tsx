import { useState } from "react";
import { api } from "../services/api";
import type { Signal, StrategyStats, Trade } from "../store/useTraderStore";
import { Badge, Direction, Empty, STATUS_LABEL, STATUS_TONE, StrategyBadge, inr, num, pnlClass, timeIST } from "./ui";

const th = "pb-2 pr-3 text-xs font-semibold text-slate-500 text-left whitespace-nowrap";
const td = "py-2 pr-3 whitespace-nowrap";

export const SignalsTable = ({ rows, empty = "No signals yet." }: { rows: Signal[]; empty?: string }) =>
  rows.length === 0 ? (
    <Empty>{empty}</Empty>
  ) : (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200">
            {["Bar close", "Strategy", "Symbol", "Direction", "Entry (index)", "Stop", "Target", "Status", "Reason / trade", "Seen"].map((h) => (
              <th key={h} className={th}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id} className="border-b border-slate-50 align-top">
              <td className={td}>{s.bar_close_ist}</td>
              <td className={td}><StrategyBadge k={s.strategy} /></td>
              <td className={`${td} font-medium`}>{s.symbol}</td>
              <td className={td}><Direction d={s.direction} /></td>
              <td className={td}>{num(s.entry_underlying)}</td>
              <td className={td}>{num(s.stop_underlying)}</td>
              <td className={td}>{num(s.target_underlying)}</td>
              <td className={td}><Badge tone={STATUS_TONE[s.status]}>{STATUS_LABEL[s.status] || s.status}</Badge></td>
              <td className="py-2 pr-3 text-xs text-slate-600 max-w-md">
                {s.trade_id ? <span className="font-medium text-slate-900">{s.trade_id} {s.meta?.tradingsymbol}</span> : s.reason}
                {s.note && <div className="text-slate-400">{s.note}</div>}
              </td>
              <td className={`${td} text-xs text-slate-400`}>{timeIST(s.detected_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

export const TradesTable = ({ rows, open = false, empty }: { rows: Trade[]; open?: boolean; empty: string }) => {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const close = async (id: string) => {
    if (!confirm(`Close paper trade ${id} at the current market price?`)) return;
    setBusy(id);
    setErr(null);
    try {
      await api.closeTrade(id);
    } catch (e: any) {
      setErr(`${id}: ${e.message}`);
    } finally {
      setBusy(null);
    }
  };
  if (rows.length === 0) return <Empty>{empty}</Empty>;
  const heads = open
    ? ["Trade", "Strategy", "Option", "Qty", "Entry", "LTP", "SL / TGT (premium)", "SL / TGT (index)", "Index LTP", "Unrealised (net)", "Flat by", ""]
    : ["Trade", "Strategy", "Option", "Qty", "Entry", "Exit", "Reason", "Gross", "Charges", "Net P&L", "In / Out"];
  return (
    <div className="overflow-x-auto">
      {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200">{heads.map((h) => <th key={h} className={th}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.id} className="border-b border-slate-50">
              <td className={`${td} font-medium`}>{t.id}</td>
              <td className={td}><StrategyBadge k={t.strategy} /></td>
              <td className={td}>
                {t.tradingsymbol} <span className="text-xs text-slate-400">{t.direction === "bullish" ? "CE" : "PE"}</span>
              </td>
              <td className={td}>{t.quantity} <span className="text-xs text-slate-400">({t.lots} lot)</span></td>
              <td className={td}>{num(t.entry_price)}</td>
              {open ? (
                <>
                  <td className={`${td} font-medium`}>{num(t.ltp)} <span className="text-[10px] text-slate-400">{timeIST(t.ltp_time)}</span></td>
                  <td className={td}>{num(t.stop_premium)} / {num(t.target_premium)}</td>
                  <td className={td}>{num(t.stop_underlying)} / {num(t.target_underlying)}</td>
                  <td className={td}>{num(t.underlying_ltp)}</td>
                  <td className={`${td} font-bold ${pnlClass(t.unrealized_net)}`}>{inr(t.unrealized_net, true)}</td>
                  <td className={td}>{t.flat_by}</td>
                  <td className={td}>
                    <button disabled={busy === t.id} onClick={() => close(t.id)} className="text-xs text-red-600 hover:underline disabled:opacity-50">
                      {busy === t.id ? "Closing…" : "Close"}
                    </button>
                  </td>
                </>
              ) : (
                <>
                  <td className={td}>{num(t.exit_price)}</td>
                  <td className={td}><Badge tone={t.exit_reason?.startsWith("TARGET") ? "green" : t.exit_reason?.startsWith("SL") ? "red" : "slate"}>{t.exit_reason}</Badge></td>
                  <td className={`${td} ${pnlClass(t.gross_pnl)}`}>{inr(t.gross_pnl, true)}</td>
                  <td className={`${td} text-slate-500`}>{inr(t.charges)}</td>
                  <td className={`${td} font-bold ${pnlClass(t.net_pnl)}`}>{inr(t.net_pnl, true)}</td>
                  <td className={`${td} text-xs text-slate-500`}>{timeIST(t.entry_time)} → {timeIST(t.exit_time)}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

type Row = [string, (s: StrategyStats) => any, ((s: StrategyStats) => string)?];
const ROWS: Row[] = [
  ["Win rate (completed)", (s) => (s.win_rate == null ? "—" : `${s.win_rate}%`)],
  ["Signals generated", (s) => s.signals],
  ["Executable signals", (s) => s.executable],
  ["After entry cutoff", (s) => s.entry_cutoff],
  ["Rejected", (s) => s.rejected],
  ["Trades executed", (s) => s.trades],
  ["Winning trades", (s) => s.wins],
  ["Losing trades", (s) => s.losses],
  ["Open trades", (s) => s.open],
  ["Net realised P&L", (s) => inr(s.net_pnl, true), (s) => `font-bold ${pnlClass(s.net_pnl)}`],
  ["Charges paid", (s) => inr(s.charges)],
  ["Best / worst trade", (s) => `${inr(s.best, true)} / ${inr(s.worst, true)}`],
];

export const StrategyComparison = ({ stats, names }: { stats: Record<string, StrategyStats>; names: Record<string, string> }) => {
  const keys = Object.keys(stats);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200">
            <th className={th}>Metric</th>
            {keys.map((k) => <th key={k} className={th}>{names[k] || k}</th>)}
          </tr>
        </thead>
        <tbody>
          {ROWS.map(([label, val, cls]) => (
            <tr key={label} className="border-b border-slate-50">
              <td className={`${td} text-slate-500`}>{label}</td>
              {keys.map((k) => <td key={k} className={`${td} ${cls ? cls(stats[k]) : ""}`}>{val(stats[k])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-xs text-slate-400 mt-2">
        Win rate = winning completed trades ÷ completed trades × 100. Open trades and signals that made no trade are not counted. P&L is net of brokerage and charges.
      </p>
    </div>
  );
};
