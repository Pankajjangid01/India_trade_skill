import { FormEvent, useEffect, useState } from "react";
import { api } from "../services/api";
import { AlertBanner, Badge, Card, Direction, Empty, StatCard, num } from "../components/ui";

type Field = { key: string; label: string; step?: string; kind?: "number" | "text" | "select"; options?: string[] };

// the strategy settings a backtest may override (the saved ones are the defaults)
const FIELDS: Record<string, Field[]> = {
  ORB: [
    { key: "stop_rule", label: "Stop rule", kind: "select", options: ["or-mid", "or-opposite"] },
    { key: "target_r", label: "Target (R)", step: "0.1" },
    { key: "max_trades_per_day", label: "Max trades / day", step: "1" },
  ],
  EMA_PULLBACK: [
    { key: "fast", label: "EMA fast", step: "1" },
    { key: "slow", label: "EMA slow", step: "1" },
    { key: "pullback_bars", label: "Pullback bars", step: "1" },
    { key: "cooldown_bars", label: "Cooldown bars", step: "1" },
    { key: "start_after", label: "Start after (HH:MM)", kind: "text" },
    { key: "min_risk_pct", label: "Min risk %", step: "0.01" },
    { key: "max_risk_pct", label: "Max risk %", step: "0.01" },
    { key: "target_r", label: "Target (R)", step: "0.1" },
    { key: "max_trades_per_day", label: "Max trades / day", step: "1" },
  ],
};

const pct = (x: number | null | undefined) => (x == null ? "—" : `${(100 * x).toFixed(1)} %`);
const r = (x: number | null | undefined) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(3)} R`);

// cumulative R by trade, with the zero line
const Equity = ({ pts }: { pts: { cum_r: number; date: string; time: string }[] }) => {
  if (pts.length < 2) return <Empty>Not enough trades for a curve.</Empty>;
  const W = 900, H = 220, P = 32;
  const ys = pts.map((p) => p.cum_r).concat([0]);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const span = hi - lo || 1;
  const x = (i: number) => P + (i / (pts.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - lo) / span) * (H - 2 * P);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.cum_r).toFixed(1)}`).join(" ");
  const end = pts[pts.length - 1].cum_r;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-56" role="img" aria-label="Equity curve in R">
      <line x1={P} x2={W - P} y1={y(0)} y2={y(0)} stroke="#cbd5e1" strokeDasharray="4 4" />
      <text x={4} y={y(0) + 4} fontSize="11" fill="#64748b">0R</text>
      <text x={4} y={y(hi) + 4} fontSize="11" fill="#64748b">{hi.toFixed(1)}R</text>
      {lo < 0 && <text x={4} y={y(lo) + 4} fontSize="11" fill="#64748b">{lo.toFixed(1)}R</text>}
      <path d={d} fill="none" stroke={end >= 0 ? "#16a34a" : "#dc2626"} strokeWidth="2" />
      <text x={P} y={H - 8} fontSize="11" fill="#64748b">{pts[0].date}</text>
      <text x={W - P} y={H - 8} fontSize="11" fill="#64748b" textAnchor="end">{pts[pts.length - 1].date}</text>
      <title>{`${pts.length} trades, ${end.toFixed(2)} R in total`}</title>
    </svg>
  );
};

const OUTCOME_TONE: Record<string, string> = { target: "green", stop: "red", flat_by: "slate" };

export const BacktestPage = () => {
  const [symbol, setSymbol] = useState("NIFTY");
  const [strategy, setStrategy] = useState("ORB");
  const [days, setDays] = useState(60);
  const [saved, setSaved] = useState<Record<string, any>>({});
  const [over, setOver] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<any>(null);

  useEffect(() => {
    api.settings().then((s) => setSaved(s.strategies || {})).catch(() => {});
  }, []);
  useEffect(() => setOver({}), [strategy]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const overrides: Record<string, unknown> = {};
      for (const f of FIELDS[strategy]) {
        const v = over[f.key];
        if (v === undefined || v === "") continue;
        overrides[f.key] = f.kind === "select" || f.kind === "text" ? v : Number(v);
      }
      setRes(await api.backtest({ symbol, strategy, days, overrides }));
    } catch (e: any) {
      setErr(e.message);
      setRes(null);
    } finally {
      setBusy(false);
    }
  };

  const inp = "border border-slate-300 rounded-md px-2 py-1.5 text-sm bg-white w-full";
  const h = res?.history;
  const b = res?.bootstrap;
  const trades = (res?.days || []).filter((t: any) => t.status === "trade");
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 mb-5">Backtest</h1>
      <Card title="Replay a strategy on past days">
        <form onSubmit={submit} className="grid grid-cols-2 md:grid-cols-6 gap-3 items-end">
          <label className="text-xs text-slate-500">Index
            <select className={inp} value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {["NIFTY", "SENSEX", "BANKNIFTY"].map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label className="text-xs text-slate-500">Strategy
            <select className={inp} value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              <option value="ORB">Opening Range Breakout</option>
              <option value="EMA_PULLBACK">EMA Trend Pullback</option>
            </select>
          </label>
          <label className="text-xs text-slate-500">Trading days (20 to 250)
            <input className={inp} type="number" min={20} max={250} value={days} onChange={(e) => setDays(Number(e.target.value))} />
          </label>
          {FIELDS[strategy].map((f) => (
            <label key={f.key} className="text-xs text-slate-500">{f.label}
              {f.kind === "select" ? (
                <select className={inp} value={over[f.key] ?? saved[strategy]?.[f.key] ?? ""}
                  onChange={(e) => setOver({ ...over, [f.key]: e.target.value })}>
                  {f.options!.map((o) => <option key={o}>{o}</option>)}
                </select>
              ) : (
                <input className={inp} type={f.kind === "text" ? "text" : "number"} step={f.step}
                  placeholder={String(saved[strategy]?.[f.key] ?? "")} value={over[f.key] ?? ""}
                  onChange={(e) => setOver({ ...over, [f.key]: e.target.value })} />
              )}
            </label>
          ))}
          <button type="submit" disabled={busy}
            className="py-2 rounded-lg bg-slate-900 text-white text-sm font-medium disabled:opacity-50">
            {busy ? "Running…" : "Run backtest"}
          </button>
        </form>
        <p className="text-xs text-slate-400 mt-3">
          Closed 5-minute index bars only (fetched once, then cached). Inside a bar the stop is checked before the target;
          not out by the flat-by time, out at that bar's close. Results are in R on the index, before option charges, and
          resampled 10,000 times (seed 19). A replay of the past, not a forecast. Empty fields use the saved settings.
        </p>
      </Card>
      {err && <AlertBanner type="error" message={err} />}
      {res && (
        <>
          {res.partial && <AlertBanner type="warning" message={`Partial data: ${res.partial}`} />}
          <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-6">
            <StatCard title="Trades" value={h.trades} sub={`${res.window.days_used} days, ${res.window.from} to ${res.window.to}`} />
            <StatCard title="Win rate" value={pct(h.win_rate)} sub={`resampled ${pct(b.win_rate.p5)} to ${pct(b.win_rate.p95)}`} />
            <StatCard title="Average per trade" value={r(h.expectancy_r)} valueClass={h.expectancy_r >= 0 ? "text-green-600" : "text-red-600"}
              sub={`resampled ${r(b.expectancy_r.p5)} to ${r(b.expectancy_r.p95)}`} />
            <StatCard title="Outcomes" value={`${h.target_first_pct}% / ${h.stop_first_pct}%`} sub={`target first / stop first; neither ${h.neither_pct}%`} />
            <StatCard title="Worst drawdown" value={`${num(h.worst_drawdown_r)} R`} sub={`resampled median ${num(b.worst_drawdown_r.median)} R`} />
            <StatCard title="Longest losing streak" value={h.longest_losing_streak} sub={`resampled 95th pct ${b.longest_losing_streak.p95}`} />
          </div>
          {b.expectancy_r.p5 < 0 && b.expectancy_r.p95 > 0 && (
            <AlertBanner type="warning" message="The resampled 5th to 95th percentile of the average crosses zero: this sample cannot tell an edge from no edge." />
          )}
          <Card title="Equity curve (cumulative R)"><Equity pts={res.equity_curve} /></Card>
          <div className="grid md:grid-cols-2 gap-6">
            <Card title="By direction">
              <table className="w-full text-sm">
                <thead><tr className="text-xs text-slate-500 text-left"><th>Direction</th><th>Trades</th><th>Win rate</th><th>Average</th></tr></thead>
                <tbody>
                  {Object.entries(res.by_direction).map(([k, v]: any) => (
                    <tr key={k}><td><Direction d={k} /></td><td>{v.trades}</td><td>{pct(v.win_rate)}</td><td>{r(v.expectancy_r)}</td></tr>
                  ))}
                </tbody>
              </table>
            </Card>
            <Card title="By month">
              <table className="w-full text-sm">
                <thead><tr className="text-xs text-slate-500 text-left"><th>Month</th><th>Trades</th><th>Win rate</th><th>Total</th></tr></thead>
                <tbody>
                  {Object.entries(res.by_month).map(([k, v]: any) => (
                    <tr key={k}><td>{k}</td><td>{v.trades}</td><td>{pct(v.win_rate)}</td>
                      <td className={v.total_r >= 0 ? "text-green-700" : "text-red-700"}>{r(v.total_r)}</td></tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </div>
          <Card title={`${trades.length} trades`}>
            <div className="overflow-x-auto max-h-[480px]">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-white">
                  <tr className="text-xs text-slate-500 text-left border-b border-slate-200">
                    {["Date", "Direction", "Entry", "Entry @", "Stop", "Target", "Outcome", "Exit @", "R"].map((x) => <th key={x} className="pb-2 pr-3">{x}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t: any, i: number) => (
                    <tr key={i} className="border-b border-slate-50">
                      <td className="py-1.5 pr-3">{t.date}{t.expiry_day && <span className="text-xs text-amber-600"> expiry</span>}</td>
                      <td className="pr-3"><Direction d={t.direction} /></td>
                      <td className="pr-3">{t.entry_time}</td>
                      <td className="pr-3">{num(t.entry)}</td>
                      <td className="pr-3">{num(t.stop)}</td>
                      <td className="pr-3">{num(t.target)}</td>
                      <td className="pr-3"><Badge tone={OUTCOME_TONE[t.outcome]}>{t.outcome === "flat_by" ? "neither (flat-by)" : t.outcome}</Badge></td>
                      <td className="pr-3">{t.exit_time}</td>
                      <td className={`pr-3 font-medium ${t.r >= 0 ? "text-green-700" : "text-red-700"}`}>{r(t.r)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </>
  );
};
