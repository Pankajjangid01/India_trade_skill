import { useEffect, useState } from "react";
import { api } from "../services/api";
import { useTraderStore, type Trade } from "../store/useTraderStore";
import { TradesTable } from "../components/tables";
import { Card, inr, pnlClass, todayIST } from "../components/ui";

export const PositionsPage = () => {
  const { snap } = useTraderStore();
  const [date, setDate] = useState(todayIST());
  const [past, setPast] = useState<Trade[] | null>(null);
  const [daily, setDaily] = useState<any[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const isToday = snap ? date === snap.today : true;

  useEffect(() => {
    if (isToday) return setPast(null);
    api.trades(date).then(setPast).catch((e) => setErr(e.message));
  }, [date, isToday]);
  const closedCount = snap?.closed_trades.length;
  useEffect(() => {
    api.daily(30).then(setDaily).catch((e) => setErr(e.message));
  }, [closedCount]);

  const closed = isToday ? snap?.closed_trades || [] : (past || []).filter((t) => t.status === "CLOSED");
  const keys = snap?.strategies.map((s) => s.key) || ["ORB", "EMA_PULLBACK"];
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 mb-5">Paper trades</h1>
      {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
      <Card title="Open positions (live)">
        <TradesTable rows={snap?.open_trades || []} open empty="No open paper positions." />
      </Card>
      <Card title={`Completed trades on ${date}`} right={<input type="date" value={date} onChange={(e) => setDate(e.target.value)} className="border border-slate-300 rounded-md px-2 py-1 text-sm" />}>
        <TradesTable rows={closed} empty="No completed trades on this date." />
      </Card>
      <Card title="Daily performance by strategy (IST trading days)">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-xs text-slate-500 text-left">
                <th className="pb-2 pr-3">Date</th>
                {keys.map((k) => <th key={k} className="pb-2 pr-3" colSpan={4}>{k}: signals / trades / win rate / net</th>)}
              </tr>
            </thead>
            <tbody>
              {daily.map((d) => (
                <tr key={d.date} className="border-b border-slate-50">
                  <td className="py-2 pr-3 font-medium">{d.date}</td>
                  {keys.map((k) => {
                    const s = d.by_strategy[k] || {};
                    return [
                      <td key={k + "s"} className="py-2 pr-3">{s.signals ?? 0}</td>,
                      <td key={k + "t"} className="py-2 pr-3">{s.trades ?? 0}{s.open ? ` (${s.open} open)` : ""}</td>,
                      <td key={k + "w"} className="py-2 pr-3">{s.win_rate == null ? "—" : `${s.win_rate}% (${s.wins}/${s.completed})`}</td>,
                      <td key={k + "p"} className={`py-2 pr-3 font-medium ${pnlClass(s.net_pnl)}`}>{inr(s.net_pnl ?? 0, true)}</td>,
                    ];
                  })}
                </tr>
              ))}
              {daily.length === 0 && <tr><td className="py-4 text-slate-500">No history yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
};
