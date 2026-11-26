import { useTraderStore } from "../store/useTraderStore";
import { EngineControl } from "../components/EngineControl";
import { SignalsTable, StrategyComparison, TradesTable } from "../components/tables";
import { AlertBanner, Badge, Card, StatCard, inr, num, pnlClass, timeIST } from "../components/ui";
import { HybridPanel } from "../components/hybrid";
import { OiPcrPanel } from "../components/oipcr";

export const Dashboard = () => {
  const { snap } = useTraderStore();
  if (!snap) return <p className="text-slate-500">Loading backend state…</p>;
  const { engine: e, angel: a, config: c } = snap;
  const names = Object.fromEntries(snap.strategies.map((s) => [s.key, s.name]));
  const realised = Object.values(snap.stats_today).reduce((x, s) => x + s.net_pnl, 0);
  const hm = timeIST(snap.server_time_ist).slice(0, 5);
  const afterCutoff = hm >= c.entry_cutoff;

  return (
    <>
      <div className="flex items-start justify-between mb-5">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">Dashboard</h1>
          <p className="text-slate-500 text-sm">
            {snap.today} • {timeIST(snap.server_time_ist)} IST • entries {c.entry_start}–{c.entry_cutoff}, flat by {c.flat_by} •
            auto paper-execute {c.auto_execute ? "on" : "off"}
          </p>
        </div>
        <EngineControl />
      </div>

      {!e.running && <AlertBanner type="warning" message="The engine is stopped: no market monitoring, no signals, open trades are not managed." />}
      {e.running && e.phase !== "open" && <AlertBanner message={`Engine running, idle: ${e.phase_reason}. It resumes by itself at the next session.`} />}
      {e.running && e.phase === "open" && afterCutoff && (
        <AlertBanner type="warning" message={`After the ${c.entry_cutoff} IST entry cutoff: signals are still recorded, no new paper trades; open trades keep their stops and targets.`} />
      )}
      {e.last_error && <AlertBanner type="error" message={`Last engine error: ${e.last_error}`} />}
      {a.last_error && <AlertBanner type="error" message={`Angel One: ${a.last_error}`} />}

      <div className="grid grid-cols-2 lg:grid-cols-6 gap-4 mb-6">
        <StatCard title="Engine" value={e.running ? "Running" : "Stopped"} valueClass={e.running ? "text-green-600" : "text-red-600"}
          sub={e.running ? `${e.cycles} cycles, last ${timeIST(e.last_cycle_ist)}` : "press Start"} />
        <StatCard title="Market monitoring" value={e.running && e.phase === "open" ? "Active" : e.phase === "open" ? "Off" : "Idle"} sub={e.phase_reason || "—"} />
        <StatCard title="Angel One" value={a.logged_in ? "Logged in" : "Offline"} valueClass={a.logged_in ? "text-green-600" : "text-red-600"}
          sub={a.session_date ? `session ${a.session_date}` : a.last_error || "—"} />
        <StatCard title="Live feed (WebSocket)" value={a.feed.connected ? "Connected" : "Down"} valueClass={a.feed.connected ? "text-green-600" : "text-red-600"}
          sub={`${a.feed.ticks.toLocaleString("en-IN")} ticks • ${a.feed.reconnects} reconnects • ${a.feed.restarts} restarts`} />
        <StatCard title="Realised P&L today" value={inr(realised, true)} valueClass={pnlClass(realised)} sub="net of charges, closed trades" />
        <StatCard title="Unrealised P&L" value={inr(snap.unrealized_net, true)} valueClass={pnlClass(snap.unrealized_net)} sub={`${snap.open_trades.length} open paper trade(s)`} />
      </div>

      <Card title="Live market data">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {snap.market.map((m) => (
            <div key={m.symbol} className="rounded-lg border border-slate-200 p-4">
              <div className="flex justify-between items-center">
                <span className="font-semibold">{m.symbol}</span>
                <span className="flex gap-1">
                  {m.expiry_today && <Badge tone="amber">expiry today</Badge>}
                  <Badge tone={m.stale ? "red" : "green"}>{m.stale ? "stale" : m.source}</Badge>
                </span>
              </div>
              <p className="text-2xl font-bold mt-1">{num(m.ltp)}</p>
              <p className={`text-sm ${pnlClass(m.change)}`}>
                {m.change == null ? "prev close —" : `${m.change > 0 ? "+" : ""}${num(m.change)} (${num(m.change_pct)}%)`}
              </p>
              <p className="text-xs text-slate-400 mt-1">
                tick {m.age_sec ?? "—"} s old • {m.bars ?? 0} closed 5-min bars ({m.bars_source || "—"}), last {m.last_bar_ist || "—"}
              </p>
            </div>
          ))}
          {snap.market.length === 0 && <p className="text-sm text-slate-500">No market data yet ({e.running ? e.phase_reason : "engine stopped"}).</p>}
        </div>
      </Card>

      <Card title="EMA + ORB Hybrid: opening range, breakout and EMA confirmation">
        <HybridPanel market={snap.market} />
      </Card>

      <Card title="OI Shift + PCR: the 09:30 battleground, writers covering, PCR confirmation">
        <OiPcrPanel market={snap.market} />
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {snap.strategies.map((s) => {
          const st = snap.stats_today[s.key];
          return (
            <div key={s.key} className="bg-white rounded-xl border border-slate-200 p-5 mb-6">
              <div className="flex justify-between items-center mb-3">
                <h2 className="font-bold">{s.name}</h2>
                <Badge tone={s.enabled ? "green" : "slate"}>{s.enabled ? "active" : "disabled"}</Badge>
              </div>
              <div className="grid grid-cols-4 gap-3 text-center">
                <div><p className="text-xs text-slate-500">Win rate</p><p className="text-xl font-bold">{st.win_rate == null ? "—" : `${st.win_rate}%`}</p></div>
                <div><p className="text-xs text-slate-500">W / L</p><p className="text-xl font-bold">{st.wins} / {st.losses}</p></div>
                <div><p className="text-xs text-slate-500">Trades (open)</p><p className="text-xl font-bold">{st.trades} ({st.open})</p></div>
                <div><p className="text-xs text-slate-500">Net P&L</p><p className={`text-xl font-bold ${pnlClass(st.net_pnl)}`}>{inr(st.net_pnl, true)}</p></div>
              </div>
              <p className="text-xs text-slate-400 mt-3">
                {st.signals} signals: {st.executable} executable, {st.entry_cutoff} after cutoff, {st.rejected} rejected
              </p>
            </div>
          );
        })}
      </div>

      <Card title="Open paper positions">
        <TradesTable rows={snap.open_trades} open empty="No open paper positions." />
      </Card>

      <Card title="Latest signals (all strategies)">
        <SignalsTable rows={snap.signals.slice(0, 12)} empty="No signals today yet." />
      </Card>

      <Card title="Completed trades today">
        <TradesTable rows={snap.closed_trades} empty="No completed trades today." />
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card title="Strategy comparison — today">
          <StrategyComparison stats={snap.stats_today} names={names} />
        </Card>
        <Card title="Strategy comparison — all time">
          <StrategyComparison stats={snap.stats_all_time} names={names} />
        </Card>
      </div>
    </>
  );
};
