import { FormEvent, ReactNode, useState } from "react";
import { api } from "../services/api";
import { AlertBanner, Badge, Card, Empty, inr, num } from "../components/ui";

const SYMS = ["NIFTY", "BANKNIFTY", "SENSEX"];
const SEGS = ["train", "validation", "test"] as const;
const COLORS: Record<string, string> = { EMA_PULLBACK: "#2563eb", EMA_ORB_B: "#d97706", EMA_ORB_C: "#16a34a", ORB: "#7c3aed" };

const r = (x: number | null | undefined, d = 3) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(d)} R`);
const pct = (x: number | null | undefined) => (x == null ? "—" : `${(100 * x).toFixed(1)}%`);
const tone = (x: number | null | undefined) => (x == null ? "" : x > 0 ? "text-green-700" : x < 0 ? "text-red-700" : "");

// shared request settings: days, indices, cost assumptions
const useRequest = () => {
  const [days, setDays] = useState(250);
  const [syms, setSyms] = useState<string[]>(SYMS);
  const [slip, setSlip] = useState("");
  const [stopSlip, setStopSlip] = useState("");
  const [prem, setPrem] = useState<Record<string, string>>({});
  const body = () => {
    const premium: Record<string, number> = {};
    for (const [k, v] of Object.entries(prem)) if (v) premium[k] = Number(v);
    return {
      days, symbols: syms, assumed_premium: premium,
      ...(slip ? { option_slippage_pct: Number(slip) } : {}),
      ...(stopSlip ? { stop_slippage_r: Number(stopSlip) } : {}),
    };
  };
  const inp = "border border-slate-300 rounded-md px-2 py-1.5 text-sm bg-white w-full";
  const form = (
    <>
      <label className="text-xs text-slate-500">Trading days (60 to 250)
        <input className={inp} type="number" min={60} max={250} value={days} onChange={(e) => setDays(Number(e.target.value))} />
      </label>
      <div className="text-xs text-slate-500">Indices
        <div className="flex gap-2 mt-1.5">
          {SYMS.map((s) => (
            <label key={s} className="flex items-center gap-1 text-slate-700">
              <input type="checkbox" checked={syms.includes(s)}
                onChange={(e) => setSyms(e.target.checked ? [...syms, s] : syms.filter((x) => x !== s))} />{s}
            </label>
          ))}
        </div>
      </div>
      <label className="text-xs text-slate-500">Option slippage % per side
        <input className={inp} type="number" step="0.1" placeholder="0.5" value={slip} onChange={(e) => setSlip(e.target.value)} />
      </label>
      <label className="text-xs text-slate-500">Extra slippage on stops (R)
        <input className={inp} type="number" step="0.01" placeholder="0.05" value={stopSlip} onChange={(e) => setStopSlip(e.target.value)} />
      </label>
      {SYMS.map((s) => (
        <label key={s} className="text-xs text-slate-500">Assumed {s} premium (Rs)
          <input className={inp} type="number" placeholder={{ NIFTY: "150", BANKNIFTY: "350", SENSEX: "300" }[s]}
            value={prem[s] || ""} onChange={(e) => setPrem({ ...prem, [s]: e.target.value })} />
        </label>
      ))}
    </>
  );
  return { body, form };
};

const Costs = ({ d }: { d: any }) => (
  <p className="text-xs text-slate-500 mt-2">
    Costs per trade in R of one lot's risk:{" "}
    {Object.entries(d.costs).map(([s, c]: any) => (
      <span key={s} className="mr-3">
        {s} {c.cost_r.toFixed(3)} R (charges {c.charges_r.toFixed(3)} at premium {c.premium} x lot {c.lot}, slippage {c.slippage_r.toFixed(3)})
      </span>
    ))}
    · plus {d.stop_slippage_r} R on every stop · 1R = {inr(d.risk_inr)}
  </p>
);

// several cumulative-R lines on one chart, with a legend
const MultiEquity = ({ series, labels }: { series: Record<string, { date: string; cum_r: number }[]>; labels: Record<string, string> }) => {
  const keys = Object.keys(series).filter((k) => series[k].length > 1);
  if (!keys.length) return <Empty>No trades to draw.</Empty>;
  const W = 900, H = 260, P = 36;
  const dates = Array.from(new Set(keys.flatMap((k) => series[k].map((p) => p.date)))).sort();
  const t = (d: string) => P + (dates.indexOf(d) / Math.max(1, dates.length - 1)) * (W - 2 * P);
  const all = keys.flatMap((k) => series[k].map((p) => p.cum_r)).concat([0]);
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const y = (v: number) => H - P - ((v - lo) / span) * (H - 2 * P);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-64" role="img" aria-label="Equity curves in R">
        <line x1={P} x2={W - P} y1={y(0)} y2={y(0)} stroke="#cbd5e1" strokeDasharray="4 4" />
        <text x={4} y={y(0) + 4} fontSize="11" fill="#64748b">0R</text>
        <text x={4} y={y(hi) + 4} fontSize="11" fill="#64748b">{hi.toFixed(0)}R</text>
        {lo < 0 && <text x={4} y={y(lo) + 4} fontSize="11" fill="#64748b">{lo.toFixed(0)}R</text>}
        {keys.map((k) => (
          <path key={k} fill="none" stroke={COLORS[k] || "#334155"} strokeWidth="2"
            d={series[k].map((p, i) => `${i ? "L" : "M"}${t(p.date).toFixed(1)},${y(p.cum_r).toFixed(1)}`).join(" ")} />
        ))}
        <text x={P} y={H - 8} fontSize="11" fill="#64748b">{dates[0]}</text>
        <text x={W - P} y={H - 8} fontSize="11" fill="#64748b" textAnchor="end">{dates[dates.length - 1]}</text>
      </svg>
      <div className="flex flex-wrap gap-4 text-xs mt-1">
        {keys.map((k) => (
          <span key={k} className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-0.5" style={{ background: COLORS[k] }} />
            {labels[k]}: <b className={tone(series[k][series[k].length - 1].cum_r)}>{r(series[k][series[k].length - 1].cum_r, 1)}</b>
          </span>
        ))}
      </div>
    </div>
  );
};

const METRICS: [string, string, (m: any) => ReactNode][] = [
  ["trades", "Trades", (m) => m.trades],
  ["wl", "Wins / losses", (m) => `${m.wins} / ${m.losses}`],
  ["win_rate", "Win rate", (m) => pct(m.win_rate)],
  ["avg_r", "Average per trade", (m) => <span className={tone(m.avg_r)}>{r(m.avg_r)}</span>],
  ["net_r", "Net after costs", (m) => <span className={tone(m.net_r)}>{r(m.net_r, 1)} ({inr(m.net_inr, true)})</span>],
  ["gp", "Gross profit / loss", (m) => `${r(m.gross_profit_r, 1)} / ${r(m.gross_loss_r, 1)}`],
  ["pf", "Profit factor", (m) => (m.profit_factor == null ? "—" : num(m.profit_factor, 2))],
  ["dd", "Max drawdown", (m) => `${num(m.max_drawdown_r, 1)} R (${inr(m.max_drawdown_inr)})`],
  ["aw", "Avg win / avg loss", (m) => `${r(m.avg_win_r, 2)} / ${r(m.avg_loss_r, 2)}`],
  ["rr", "Reward : risk", (m) => (m.reward_risk == null ? "—" : num(m.reward_risk, 2))],
  ["streak", "Max consecutive W / L", (m) => `${m.max_consecutive_wins} / ${m.max_consecutive_losses}`],
  ["costs", "Costs paid", (m) => r(-m.costs_r, 1)],
];

const MetricsTable = ({ src, versions }: { src: Record<string, any>; versions: any[] }) => (
  <div className="overflow-x-auto">
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-slate-200 text-xs text-slate-500 text-left">
          <th className="pb-2 pr-3">Metric (whole period)</th>
          {versions.map((v) => <th key={v.key} className="pb-2 pr-3" style={{ color: COLORS[v.key] }}>{v.label}</th>)}
        </tr>
      </thead>
      <tbody>
        {METRICS.map(([k, label, f]) => (
          <tr key={k} className="border-b border-slate-50">
            <td className="py-1.5 pr-3 text-slate-600">{label}</td>
            {versions.map((v) => <td key={v.key} className="pr-3">{src[v.key]?.all?.trades ? f(src[v.key].all) : "—"}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

const SegmentTable = ({ src, versions, ranges }: { src: Record<string, any>; versions: any[]; ranges?: any }) => (
  <table className="w-full text-sm">
    <thead>
      <tr className="border-b border-slate-200 text-xs text-slate-500 text-left">
        <th className="pb-2 pr-3">Version</th>
        {SEGS.map((s) => (
          <th key={s} className="pb-2 pr-3 capitalize">{s}{ranges?.[s] ? <span className="font-normal"> {ranges[s][0]} to {ranges[s][1]}</span> : ""}</th>
        ))}
      </tr>
    </thead>
    <tbody>
      {versions.map((v) => (
        <tr key={v.key} className="border-b border-slate-50">
          <td className="py-1.5 pr-3" style={{ color: COLORS[v.key] }}>{v.label}</td>
          {SEGS.map((s) => {
            const m = src[v.key]?.[s];
            return (
              <td key={s} className="pr-3">
                {m?.trades ? (
                  <>
                    <span className={`font-medium ${tone(m.avg_r)}`}>{r(m.avg_r)}</span>
                    <span className="text-xs text-slate-500"> · {m.trades} tr · win {pct(m.win_rate)} · PF {m.profit_factor == null ? "—" : num(m.profit_factor, 2)}</span>
                  </>
                ) : "—"}
              </td>
            );
          })}
        </tr>
      ))}
    </tbody>
  </table>
);

const Monthly = ({ src, versions }: { src: Record<string, any>; versions: any[] }) => {
  const months = Array.from(new Set(versions.flatMap((v) => Object.keys(src[v.key]?.all?.monthly_r || {})))).sort();
  if (!months.length) return <Empty>No trades.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-slate-500 text-left border-b border-slate-200">
            <th className="pb-2 pr-3">Month</th>
            {versions.map((v) => <th key={v.key} className="pb-2 pr-3" style={{ color: COLORS[v.key] }}>{v.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {months.map((mo) => (
            <tr key={mo} className="border-b border-slate-50">
              <td className="py-1 pr-3">{mo}</td>
              {versions.map((v) => {
                const x = src[v.key]?.all?.monthly_r?.[mo];
                const inrx = src[v.key]?.all?.monthly_inr?.[mo];
                return <td key={v.key} className={`pr-3 ${tone(x)}`}>{x == null ? "—" : `${r(x, 1)} (${inr(inrx, true)})`}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export const CompareBacktest = () => {
  const req = useRequest();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [d, setD] = useState<any>(null);
  const [scope, setScope] = useState("COMBINED");
  const run = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      setD(await api.compare(req.body()));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const labels = Object.fromEntries((d?.versions || []).map((v: any) => [v.key, v.label]));
  const src = d ? (scope === "COMBINED" ? d.combined : d.by_index[scope] || {}) : {};
  const eq = d ? Object.fromEntries((d.versions || []).map((v: any) => [v.key, d.equity[scope === "COMBINED" ? v.key : `${scope} ${v.key}`] || []])) : {};
  return (
    <>
      <Card title="Compare the strategy versions">
        <form onSubmit={run} className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
          {req.form}
          <button type="submit" disabled={busy} className="py-2 rounded-lg bg-slate-900 text-white text-sm font-medium disabled:opacity-50">
            {busy ? "Running (about 20 s)…" : "Run comparison"}
          </button>
        </form>
        <p className="text-xs text-slate-400 mt-3">
          A: the existing EMA Pullback. B: EMA Pullback only after an opening-range breakout. C: B on days whose opening range is
          at least the hybrid's min_or_pct. ORB standalone. Entries at the next bar's open, a gap through a stop fills at the open,
          costs and slippage taken off every trade, each index split 50% train / 25% validation / 25% test in time order.
          Index moves in R, not option P&L.
        </p>
      </Card>
      {err && <AlertBanner type="error" message={err} />}
      {d && (
        <>
          <AlertBanner type="warning" message={d.options} />
          {Object.keys(d.errors).length > 0 && <AlertBanner type="warning" message={Object.entries(d.errors).map(([k, v]) => `${k}: ${v}`).join("; ")} />}
          <div className="flex gap-2 mb-4">
            {["COMBINED", ...d.symbols].map((s: string) => (
              <button key={s} onClick={() => setScope(s)}
                className={`px-3 py-1 rounded-full text-xs font-medium border ${scope === s ? "bg-slate-900 text-white border-slate-900" : "border-slate-300 text-slate-600"}`}>
                {s === "COMBINED" ? "All indices combined" : s}
              </button>
            ))}
          </div>
          <Card title="Train / validation / test: average R per trade after costs">
            <SegmentTable src={src} versions={d.versions} ranges={scope === "COMBINED" ? d.segments[d.symbols[0]] : d.segments[scope]} />
            <p className="text-xs text-slate-400 mt-2">
              A version worth trusting is positive in every period, above all in the test period nobody tuned on.
            </p>
          </Card>
          <Card title="Equity curves (cumulative R after costs)"><MultiEquity series={eq} labels={labels} /></Card>
          <Card title="All metrics"><MetricsTable src={src} versions={d.versions} /><Costs d={d} /></Card>
          <Card title="Monthly P&L"><Monthly src={src} versions={d.versions} /></Card>
          <Card title="Data checks">
            <div className="text-xs text-slate-600 space-y-1">
              {Object.entries(d.data).map(([s, q]: any) => (
                <p key={s}>
                  <b>{s}</b>: {q.days} days with bars ({q.span[0]} to {q.span[1]}); empty {q.empty_days.length}, incomplete {q.incomplete_days.length},
                  no opening range {q.missing_opening_range.length}, failed fetch {q.failed_fetch.length}; trades skipped (next open past the stop or target):{" "}
                  {Object.entries(d.skipped[s] || {}).map(([k, v]) => `${k} ${v}`).join(", ")}
                </p>
              ))}
            </div>
          </Card>
        </>
      )}
    </>
  );
};

export const SweepBacktest = () => {
  const req = useRequest();
  const [version, setVersion] = useState("EMA_ORB_C");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [d, setD] = useState<any>(null);
  const run = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      setD(await api.sweep({ ...req.body(), version }));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const cell = (m: any) =>
    m?.trades ? (
      <>
        <span className={`font-medium ${tone(m.avg_r)}`}>{r(m.avg_r)}</span>
        <span className="text-xs text-slate-500"> · {m.trades} · PF {m.profit_factor == null ? "—" : num(m.profit_factor, 2)} · DD {num(m.max_drawdown_r, 1)}R · win {pct(m.win_rate)}</span>
      </>
    ) : "—";
  return (
    <>
      <Card title="Optimize the hybrid's target and stop">
        <form onSubmit={run} className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
          <label className="text-xs text-slate-500">Version
            <select className="border border-slate-300 rounded-md px-2 py-1.5 text-sm bg-white w-full" value={version} onChange={(e) => setVersion(e.target.value)}>
              <option value="EMA_ORB_C">C: EMA + ORB + min range</option>
              <option value="EMA_ORB_B">B: EMA + ORB</option>
            </select>
          </label>
          {req.form}
          <button type="submit" disabled={busy} className="py-2 rounded-lg bg-slate-900 text-white text-sm font-medium disabled:opacity-50">
            {busy ? "Running (about 40 s)…" : "Run sweep"}
          </button>
        </form>
        <p className="text-xs text-slate-400 mt-3">
          Targets 1R to 3R x stop methods (ema, atr, swing), all chosen indices pooled. The choice is made on the train period
          only and must stay above break-even on validation; the test period is shown for the chosen setting alone, so it was never
          used to pick. The risk band (min/max risk %) is the same for every stop: a wider stop never qualifies a trade.
        </p>
      </Card>
      {err && <AlertBanner type="error" message={err} />}
      {d && (
        <Card title={d.chosen ? `Chosen: ${d.chosen.stop_method} stop, ${d.chosen.target_r}R target` : "No setting passed validation"}>
          {!d.chosen && <AlertBanner type="warning" message="No target and stop combination was above break-even on both train and validation: keep the current settings." />}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-xs text-slate-500 text-left">
                  <th className="pb-2 pr-3">Stop</th><th className="pb-2 pr-3">Target</th>
                  <th className="pb-2 pr-3">Train</th><th className="pb-2 pr-3">Validation</th><th className="pb-2 pr-3">Test (chosen only)</th>
                </tr>
              </thead>
              <tbody>
                {d.rows.map((x: any) => (
                  <tr key={x.stop_method + x.target_r} className={`border-b border-slate-50 ${x.chosen ? "bg-green-50" : ""}`}>
                    <td className="py-1.5 pr-3">{x.stop_method}</td>
                    <td className="pr-3">{x.target_r}R {x.chosen && <Badge tone="green">chosen</Badge>}</td>
                    <td className="pr-3">{cell(x.train)}</td>
                    <td className="pr-3">{cell(x.validation)}</td>
                    <td className="pr-3">{x.test ? cell(x.test) : <span className="text-slate-300">hidden</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-slate-500 mt-2">{d.rule}</p>
          <Costs d={{ ...d, risk_inr: null }} />
          <AlertBanner type="warning" message={d.options} />
        </Card>
      )}
    </>
  );
};
