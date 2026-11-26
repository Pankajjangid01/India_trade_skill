import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../services/api";
import { AlertBanner, Badge, Card, Empty, StatCard, inr, num } from "../components/ui";

// Machine Learning: Lorentzian Classification (backend app/lorentzian.py): the live model of one index, its chart,
// today's entries with the reason each was or was not confirmed, and the four-configuration volume backtest.

type Rec = {
  date: string; start_ist: string; open: number; high: number; low: number; close: number; prediction: number;
  color: number; kernel: number | null; kernel_dir: string | null; signal: number; start_long: boolean;
  start_short: boolean; confirmed_long: boolean; confirmed_short: boolean; end_long: boolean; end_short: boolean;
  volume: number | null; rvol: number | null;
};
type Latest = Rec & {
  direction: string | null; neighbors: number; features: (number | null)[]; bars_held: number; exit_mode: string;
  volume_ma: number | null; bull_volume: number | null; bear_volume: number | null; volume_dir: number;
  volume_reject_long: string | null; volume_reject_short: string | null;
  filters: Record<string, any> & { raw: Record<string, any> };
};
type View = {
  symbol: string; enabled: boolean; traded: boolean; settings: Record<string, any>; feature_names: string[];
  latest: Latest | null; bars: Rec[]; date?: string; as_of_ist?: string; error?: string | null; note?: string;
  data?: Record<string, any>;
  decisions: { bar_close_ist: string; direction: string; accepted: boolean; reason: string; prediction: number }[];
  exits: { bar_start_ist: string; direction: string; mode: string }[];
};

const INDICES = ["NIFTY", "SENSEX", "BANKNIFTY"];
const GREEN = "0,153,136", RED = "204,51,17";
const tint = (c: number) => (c > 0 ? `rgba(${GREEN},${0.25 + 0.65 * c})` : c < 0 ? `rgba(${RED},${0.25 - 0.65 * c})` : "#94a3b8");
const pct = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);

// candles coloured by the prediction (the original's bar colours), the kernel estimate, entries and exits
const Chart = ({ bars, day }: { bars: Rec[]; day?: string }) => {
  if (bars.length === 0) return <Empty>No closed bars yet.</Empty>;
  const W = 10, H = 300, PAD = 24;
  const ys = bars.flatMap((b) => [b.high, b.low, ...(b.kernel != null ? [b.kernel] : [])]);
  const hi = Math.max(...ys), lo = Math.min(...ys), span = Math.max(hi - lo, 1);
  const y = (v: number) => PAD + ((hi - v) / span) * (H - 2 * PAD);
  const width = bars.length * W + 50;
  const firstToday = bars.findIndex((b) => b.date === day);
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${width} ${H}`} className="w-full min-w-[640px] h-[300px]" role="img"
        aria-label="5-minute candles coloured by the Lorentzian prediction, with the kernel estimate and signals">
        {[hi, (hi + lo) / 2, lo].map((v) => (
          <g key={v}>
            <line x1={0} x2={width - 50} y1={y(v)} y2={y(v)} stroke="#e2e8f0" />
            <text x={width - 46} y={y(v) + 4} fontSize="10" fill="#64748b">{num(v, 0)}</text>
          </g>
        ))}
        {firstToday > 0 && (
          <g>
            <line x1={firstToday * W} x2={firstToday * W} y1={0} y2={H} stroke="#94a3b8" strokeDasharray="3 3" />
            <text x={firstToday * W + 3} y={12} fontSize="10" fill="#64748b">today</text>
          </g>
        )}
        {bars.map((b, i) => {
          const x = i * W + W / 2, up = b.close >= b.open, fill = tint(b.color);
          return (
            <g key={b.date + b.start_ist} opacity={b.date === day ? 1 : 0.45}>
              <title>{`${b.date} ${b.start_ist}  O ${num(b.open)} H ${num(b.high)} L ${num(b.low)} C ${num(b.close)}\nprediction ${b.prediction}  kernel ${b.kernel_dir ?? "—"}  RVOL ${b.rvol ?? "—"}`}</title>
              <line x1={x} x2={x} y1={y(b.high)} y2={y(b.low)} stroke={fill} />
              <rect x={x - 3.5} width={7} y={y(Math.max(b.open, b.close))} height={Math.max(1, Math.abs(y(b.open) - y(b.close)))}
                fill={up ? fill : "white"} stroke={fill} />
              {b.start_long && (
                <path d={`M${x - 4},${y(b.low) + 12} L${x + 4},${y(b.low) + 12} L${x},${y(b.low) + 5} Z`}
                  fill={b.confirmed_long ? `rgb(${GREEN})` : "white"} stroke={`rgb(${GREEN})`} />
              )}
              {b.start_short && (
                <path d={`M${x - 4},${y(b.high) - 12} L${x + 4},${y(b.high) - 12} L${x},${y(b.high) - 5} Z`}
                  fill={b.confirmed_short ? `rgb(${RED})` : "white"} stroke={`rgb(${RED})`} />
              )}
              {(b.end_long || b.end_short) && (
                <text x={x - 3} y={y(b.close) + 3} fontSize="9" fill="#0f172a">✕</text>
              )}
            </g>
          );
        })}
        {bars.slice(1).map((b, i) => {
          const a = bars[i];
          if (a.kernel == null || b.kernel == null) return null;
          return (
            <line key={"k" + i} x1={i * W + W / 2} x2={(i + 1) * W + W / 2} y1={y(a.kernel)} y2={y(b.kernel)} strokeWidth={2}
              stroke={b.kernel_dir === "bullish" ? `rgb(${GREEN})` : b.kernel_dir === "bearish" ? `rgb(${RED})` : "#94a3b8"} />
          );
        })}
      </svg>
      <div className="flex flex-wrap gap-4 text-[11px] text-slate-500 mt-1">
        <span>Candle colour: prediction (green +, red −, stronger = more neighbours agree)</span>
        <span>Line: kernel estimate (green rising, red falling)</span>
        <span>▲▼ filled: confirmed entry · hollow: rejected by volume · ✕ indicator exit</span>
      </div>
    </div>
  );
};

const Status = ({ on, pass, label, detail }: { on: boolean; pass: boolean | null; label: string; detail?: string }) => (
  <div className="flex items-center justify-between py-1.5 border-b border-slate-100 last:border-0 text-sm">
    <span>{label}</span>
    <span className="flex items-center gap-2">
      {detail && <span className="text-xs text-slate-500">{detail}</span>}
      {!on ? <Badge tone="slate">OFF</Badge> : pass == null ? <Badge tone="slate">—</Badge> :
        <Badge tone={pass ? "green" : "red"}>{pass ? "PASS" : "FAIL"}</Badge>}
    </span>
  </div>
);

const SEGMENTS = ["all", "train", "validation", "test"] as const;

const BacktestPanel = ({ symbol }: { symbol: string }) => {
  const [days, setDays] = useState(40);
  const [job, setJob] = useState<any>(null);
  const [seg, setSeg] = useState<(typeof SEGMENTS)[number]>("test");
  const [err, setErr] = useState<string | null>(null);
  const poll = useCallback(() => api.lcBacktest().then(setJob).catch((e) => setErr(e.message)), []);
  useEffect(() => { poll(); }, [poll]);
  useEffect(() => {
    if (job?.status !== "running") return;
    const t = setInterval(poll, 2000);
    return () => clearInterval(t);
  }, [job?.status, poll]);
  const run = () => { setErr(null); api.lcBacktestStart(symbol, days).then(setJob).catch((e) => setErr(e.message)); };
  const r = job?.status === "done" ? job.result : null;
  return (
    <Card title="Backtest: four volume configurations, same days, out of sample"
      right={
        <div className="flex items-center gap-2 text-sm">
          <label className="flex items-center gap-1">Days
            <input type="number" min={10} max={120} value={days} onChange={(e) => setDays(Number(e.target.value))}
              className="w-16 px-2 py-1 border border-slate-300 rounded" />
          </label>
          <button onClick={run} disabled={job?.status === "running"}
            className="px-3 py-1.5 rounded bg-slate-900 text-white disabled:opacity-50">
            {job?.status === "running" ? `Running ${job.progress?.[0] ?? 0}/${job.progress?.[1] ?? "?"}` : `Run on ${symbol}`}
          </button>
        </div>
      }>
      {err && <AlertBanner type="error" message={err} />}
      {job?.status === "error" && <AlertBanner type="error" message={job.error} />}
      {!r ? (
        <p className="text-sm text-slate-500">
          Each test day rebuilds the model exactly as live does (2,000 bars of history, then that day) and fills trades like
          the other backtests: entry at the next bar's open, stop before target, the indicator's exit at a bar close,
          option charges and slippage in R. Volume is the near-month futures'; only days with it are used, for all four.
          {job?.status === "done" && job.symbol !== symbol && " (The last result is for another index.)"}
        </p>
      ) : (
        <BacktestResult r={r} seg={seg} setSeg={setSeg} />
      )}
    </Card>
  );
};

type Seg = (typeof SEGMENTS)[number];

export const BacktestResult = ({ r, seg, setSeg }: { r: any; seg: Seg; setSeg: (s: Seg) => void }) => (
        <>
          <div className="flex flex-wrap items-center gap-2 mb-3 text-xs">
            {SEGMENTS.map((s) => (
              <button key={s} onClick={() => setSeg(s)}
                className={`px-2.5 py-1 rounded border ${seg === s ? "bg-slate-900 text-white border-slate-900" : "border-slate-300"}`}>
                {s}{s !== "all" && r.segments?.[s] ? ` ${r.segments[s][0]} → ${r.segments[s][1]}` : ""}
              </button>
            ))}
            <span className="text-slate-500">{r.symbol} · {r.days_used} days with futures volume ({r.futures}) · 1R = {inr(r.risk_inr)}</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-500 text-left">
                <tr>
                  <th className="py-1.5">Configuration</th><th>Trades</th><th>Win rate</th><th>Net after charges</th>
                  <th>Profit factor</th><th>Avg R / trade</th><th>Max drawdown</th>
                </tr>
              </thead>
              <tbody>
                {r.configs.map((c: any) => {
                  const m = r.by_config[c.key]?.[seg] || {};
                  return (
                    <tr key={c.key} className="border-t border-slate-100">
                      <td className="py-1.5">{c.label}</td>
                      <td>{m.trades ?? 0}</td>
                      <td>{pct(m.win_rate)}</td>
                      <td className={m.net_inr > 0 ? "text-green-700" : m.net_inr < 0 ? "text-red-700" : ""}>
                        {inr(m.net_inr, true)} <span className="text-xs text-slate-400">({num(m.net_r, 2)} R)</span>
                      </td>
                      <td>{m.profit_factor == null ? "—" : num(m.profit_factor, 2)}</td>
                      <td>{num(m.avg_r, 3)}</td>
                      <td>{num(m.max_drawdown_r, 2)} R <span className="text-xs text-slate-400">({inr(m.max_drawdown_inr)})</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="mt-3 space-y-1">
            {r.verdict.map((v: string) => (
              <p key={v} className="text-sm">{v.includes("better than config 1 in BOTH") ? "✅" : "•"} {v}</p>
            ))}
          </div>
          <p className="text-xs text-slate-500 mt-2">{r.note} Train = first 50% of days, validation = next 25%, test = last 25%;
            nothing is fitted on any of them. A difference is only called with at least 30 trades in each out-of-sample period.</p>
        </>
);

export const LorentzianPage = () => {
  const [symbol, setSymbol] = useState("NIFTY");
  const [view, setView] = useState<View | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback((build = false) => {
    setBusy(build);
    return api.lorentzian(symbol, build).then((v) => { setView(v); setErr(null); })
      .catch((e) => setErr(e.message)).finally(() => setBusy(false));
  }, [symbol]);
  useEffect(() => {
    load();
    const t = setInterval(() => load(), 30000);                 // a new bar every 5 minutes; 30 s is plenty
    return () => clearInterval(t);
  }, [load]);

  const s = view?.settings || {};
  return (
    <div className="space-y-6">
      <Card title="Machine Learning: Lorentzian Classification"
        right={
          <div className="flex items-center gap-2">
            {INDICES.map((x) => (
              <button key={x} onClick={() => setSymbol(x)}
                className={`px-3 py-1.5 rounded text-sm border ${symbol === x ? "bg-slate-900 text-white border-slate-900" : "border-slate-300"}`}>{x}</button>
            ))}
            <button onClick={() => load(true)} disabled={busy} className="px-3 py-1.5 rounded text-sm border border-slate-300">
              {busy ? "Building…" : "Build now"}
            </button>
          </div>
        }>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={view?.enabled ? "green" : "slate"}>{view?.enabled ? "strategy on" : "strategy off"}</Badge>
          <Badge tone={view?.traded ? "blue" : "slate"}>{view?.traded ? `trades ${symbol}` : `${symbol} not in its symbols`}</Badge>
          <Badge>5-minute closed bars</Badge>
          <Badge>source {s.source ?? "hlc3"}</Badge>
          {view?.data && (
            <span className="text-xs text-slate-500">
              {view.data.history_bars} history bars ({view.data.history_from} → {view.data.history_to}),
              {" "}{view.data.history_volume_bars} with futures volume ({view.data.futures ?? "no futures"}) · {view.data.today_bars} bars today
            </span>
          )}
          <Link to="/settings" className="text-xs text-blue-700 underline ml-auto">Edit settings (LORENTZIAN)</Link>
        </div>
        {view?.note && <p className="text-sm text-slate-500 mt-2">{view.note}</p>}
        {(err || view?.error) && <div className="mt-2"><AlertBanner type="error" message={err || view?.error} /></div>}
      </Card>

      {view?.latest && <LiveView view={view} symbol={symbol} />}
      <BacktestPanel symbol={symbol} />
    </div>
  );
};

// the live model's last bar, the chart, filters, features, volume and today's decisions
export const LiveView = ({ view, symbol }: { view: View; symbol: string }) => {
  const l = view.latest!, s = view.settings, f = l.filters;
  const finalSig = l.confirmed_long ? "BUY" : l.confirmed_short ? "SELL" : l.start_long || l.start_short ? "rejected" : "none";
  return (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
            <StatCard title="Prediction score" value={`${l.prediction > 0 ? "+" : ""}${l.prediction}`}
              valueClass={l.prediction > 0 ? "text-green-700" : l.prediction < 0 ? "text-red-700" : ""}
              sub={l.direction ? `${l.direction} bias` : "no clear bias"} />
            <StatCard title="Neighbors" value={`${l.neighbors} / ${s.neighbors_count}`} sub={`of ${s.max_bars_back} bars back, Lorentzian distance`} />
            <StatCard title="Filtered signal" value={l.signal === 1 ? "Long" : l.signal === -1 ? "Short" : "Neutral"}
              sub={`held ${l.bars_held} bar(s) · at ${l.start_ist}`} />
            <StatCard title="Kernel" value={l.kernel_dir ?? "—"} sub={`estimate ${num(l.kernel)}`}
              valueClass={l.kernel_dir === "bullish" ? "text-green-700" : l.kernel_dir === "bearish" ? "text-red-700" : ""} />
            <StatCard title="Signal on the last bar" value={finalSig}
              valueClass={finalSig === "BUY" ? "text-green-700" : finalSig === "SELL" ? "text-red-700" : ""}
              sub={l.volume_reject_long || l.volume_reject_short || (finalSig === "none" ? "no new entry" : "")} />
            <StatCard title="Exit signal" value={l.end_long ? "Exit long" : l.end_short ? "Exit short" : "none"}
              sub={`${l.exit_mode} exits${s.exit_on_flip ? " + on a flip" : ""}`} />
          </div>

          <Card title={`${symbol} 5-minute bars: prediction colours and the kernel estimate`}>
            <Chart bars={view!.bars} day={view!.date} />
          </Card>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <Card title="Filters (the last bar)">
              <Status on={s.use_volatility_filter} pass={f!.volatility} label="Volatility (ATR 1 > ATR 10)" />
              <Status on={s.use_regime_filter} pass={f!.regime} label={`Regime (threshold ${s.regime_threshold})`}
                detail={f!.raw.regime_slope != null ? `slope ${f!.raw.regime_slope}` : undefined} />
              <Status on={s.use_adx_filter} pass={f!.raw.adx_ok} label={`ADX 14 > ${s.adx_threshold}`}
                detail={f!.raw.adx != null ? `ADX ${f!.raw.adx}` : undefined} />
              <Status on={s.use_ema_filter} pass={s.use_ema_filter ? (f!.ema_up ? true : f!.ema_down ? false : null) : null}
                label={`EMA ${s.ema_period} trend`} detail={f!.raw.ema != null ? `${f!.ema_up ? "above" : "below"} ${num(f!.raw.ema)}` : undefined} />
              <Status on={s.use_sma_filter} pass={s.use_sma_filter ? (f!.sma_up ? true : f!.sma_down ? false : null) : null}
                label={`SMA ${s.sma_period} trend`} detail={f!.raw.sma != null ? `${f!.sma_up ? "above" : "below"} ${num(f!.raw.sma)}` : undefined} />
              <Status on={s.use_kernel_filter} pass={l.kernel_dir ? true : null} label="Kernel confirmation"
                detail={l.kernel_dir ? `${l.kernel_dir} (buys need bullish, sells bearish)` : undefined} />
              <Status on={s.use_volume_filter} pass={l.rvol != null ? l.rvol >= s.min_rvol : null} label={`Volume (RVOL ≥ ${s.min_rvol})`}
                detail={l.rvol != null ? `RVOL ${num(l.rvol)}` : "no futures volume"} />
            </Card>

            <Card title="Features (normalized, the last bar)">
              <table className="w-full text-sm">
                <tbody>
                  {view!.feature_names.map((n, i) => (
                    <tr key={n} className="border-b border-slate-100 last:border-0">
                      <td className="py-1.5">{n}</td>
                      <td className="text-right font-mono">{l.features[i] == null ? "warming up" : l.features[i]!.toFixed(4)}</td>
                      <td className="w-24 pl-3">
                        <div className="h-1.5 bg-slate-100 rounded">
                          <div className="h-1.5 bg-slate-500 rounded" style={{ width: `${Math.max(0, Math.min(1, l.features[i] ?? 0)) * 100}%` }} />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-[11px] text-slate-400 mt-2">RSI and CCI read the close, WaveTrend hlc3, ADX high/low/close, as in the original.</p>
            </Card>

            <Card title="Volume (near-month futures)">
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div><p className="text-slate-500 text-xs">Current volume</p><p className="font-medium">{num(l.volume, 0)}</p></div>
                <div><p className="text-slate-500 text-xs">Average ({s.volume_ma_period} bars)</p><p className="font-medium">{num(l.volume_ma, 0)}</p></div>
                <div><p className="text-slate-500 text-xs">Relative volume</p>
                  <p className={`font-medium ${l.rvol != null && l.rvol >= s.min_rvol ? "text-green-700" : ""}`}>{num(l.rvol)}</p></div>
                <div><p className="text-slate-500 text-xs">Futures candle</p><p className="font-medium">{l.volume_dir > 0 ? "bullish" : l.volume_dir < 0 ? "bearish" : "flat"}</p></div>
                <div><p className="text-slate-500 text-xs">Bullish candle volume</p><p className="font-medium">{num(l.bull_volume, 0)}</p></div>
                <div><p className="text-slate-500 text-xs">Bearish candle volume</p><p className="font-medium">{num(l.bear_volume, 0)}</p></div>
              </div>
              <div className="flex gap-2 mt-3">
                <Badge tone={s.use_volume_filter ? "blue" : "slate"}>volume filter {s.use_volume_filter ? "ON" : "OFF"}</Badge>
                <Badge tone={s.use_rvol_feature ? "blue" : "slate"}>RVOL feature {s.use_rvol_feature ? "ON" : "OFF"}</Badge>
              </div>
              <p className="text-[11px] text-slate-400 mt-2">Index candles have no volume: these are the futures' bars at the same times.</p>
            </Card>
          </div>

          <Card title="Entries today and why each was or was not taken">
            {view!.decisions.length === 0 ? <Empty>The indicator has made no new entry today.</Empty> : (
              <ul className="space-y-1">
                {view!.decisions.slice().reverse().map((d) => (
                  <li key={d.bar_close_ist + d.direction} className="text-sm">
                    <span className={d.accepted ? "text-green-700 font-medium" : "text-slate-600"}>
                      {d.bar_close_ist} {d.direction === "bullish" ? "▲ BUY" : "▼ SELL"} (prediction {d.prediction > 0 ? "+" : ""}{d.prediction}) {d.accepted ? "confirmed" : "rejected"}
                    </span>
                    <span className="text-slate-500"> — {d.reason}{!d.accepted ? " (insufficient volume confirmation)" : ""}</span>
                  </li>
                ))}
              </ul>
            )}
            {view!.exits.length > 0 && (
              <p className="text-xs text-slate-500 mt-2">Indicator exits today: {view!.exits.map((x) => `${x.bar_start_ist} (${x.direction}, ${x.mode})`).join(", ")}</p>
            )}
            <p className="text-xs text-slate-400 mt-2">A confirmed entry still passes the paper gate (09:30–14:00, limits) and in-risk before a paper trade opens.</p>
          </Card>
        </>
  );
};
