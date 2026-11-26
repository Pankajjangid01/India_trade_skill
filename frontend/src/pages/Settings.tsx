import { useEffect, useState } from "react";
import { api } from "../services/api";
import { Card } from "../components/ui";

// Loaded from and saved to the backend (trader/config.json). Nothing is kept in the browser.
const FIELDS: Record<string, [string, string, string][]> = {
  account: [
    ["capital_inr", "Capital (₹)", "number"],
    ["risk_per_trade_pct", "Max loss per trade (% of capital)", "number"],
    ["max_daily_loss_pct", "Daily loss limit (% of capital, realised)", "number"],
    ["max_trades_per_day", "Max trades per day (all strategies)", "number"],
    ["max_consecutive_losses", "Max losses in a row (today)", "number"],
    ["max_capital_per_trade_pct", "Max capital per trade (%)", "number"],
  ],
  session: [
    ["entry_start", "Entries from (IST)", "time"],
    ["entry_end", "Entry cutoff (IST): no new trades after", "time"],
    ["flat_by", "Flat by (IST)", "time"],
    ["expiry_day_flat_by", "Expiry-day flat by (IST)", "time"],
  ],
  exit: [["max_premium_loss_pct", "Premium stop (% below entry)", "number"]],
};

export const SettingsPage = () => {
  const [s, setS] = useState<any>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.settings().then(setS).catch((e) => setMsg({ ok: false, text: e.message }));
  }, []);

  if (!s) return msg ? <p className="text-red-600">{msg.text}</p> : <p className="text-slate-500">Loading settings…</p>;

  const set = (sec: string, k: string, v: any, strat?: string) =>
    setS((o: any) =>
      strat
        ? { ...o, strategies: { ...o.strategies, [strat]: { ...o.strategies[strat], [k]: v } } }
        : { ...o, [sec]: { ...o[sec], [k]: v } },
    );
  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const strategies = Object.fromEntries(
        Object.entries(s.strategies).map(([k, v]: any) => [k, Object.fromEntries(Object.entries(v).filter(([f]) => f !== "name"))]),
      );
      const saved = await api.saveSettings({ account: s.account, session: s.session, exit: s.exit, paper: s.paper, strategies });
      setS(saved);
      setMsg({ ok: true, text: "Saved by the backend. The engine applies it from its next cycle." });
    } catch (e: any) {
      setMsg({ ok: false, text: e.message });
    } finally {
      setSaving(false);
    }
  };
  const input = "w-full px-3 py-2 border border-slate-300 rounded-md text-sm";
  const field = (sec: string, k: string, label: string, type: string, strat?: string) => {
    const v = strat ? s.strategies[strat][k] : s[sec][k];
    return (
      <label key={k} className="block">
        <span className="text-sm text-slate-700">{label}</span>
        {k === "stop_method" ? (
          <select value={v} onChange={(e) => set(sec, k, e.target.value, strat)} className={input}>
            <option value="ema">ema (the EMA setup's swing over the pullback)</option>
            <option value="atr">atr (atr_mult x ATR14)</option>
            <option value="swing">swing (lowest low / highest high of swing_bars)</option>
          </select>
        ) : typeof v === "boolean" ? (
          <input type="checkbox" checked={v} onChange={(e) => set(sec, k, e.target.checked, strat)} className="ml-2" />
        ) : (
          <input
            type={type}
            step="any"
            value={v ?? ""}
            onChange={(e) => set(sec, k, type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value, strat)}
            className={input}
          />
        )}
      </label>
    );
  };

  return (
    <>
      <div className="flex justify-between items-center mb-5">
        <h1 className="text-2xl font-bold text-slate-900">Settings</h1>
        <button onClick={save} disabled={saving} className="bg-indian-green text-white px-4 py-2 rounded-lg text-sm font-medium disabled:opacity-50">
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
      {msg && <p className={`text-sm mb-4 ${msg.ok ? "text-green-700" : "text-red-600"}`}>{msg.text}</p>}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card title="Account & risk limits">
          <div className="space-y-3">{FIELDS.account.map(([k, l, t]) => field("account", k, l, t))}</div>
        </Card>
        <div>
          <Card title="Session (IST) & exits">
            <div className="space-y-3">
              {FIELDS.session.map(([k, l, t]) => field("session", k, l, t))}
              {FIELDS.exit.map(([k, l, t]) => field("exit", k, l, t))}
            </div>
          </Card>
          <Card title="Paper trading">
            <label className="block text-sm text-slate-700 mb-3">
              <input type="checkbox" checked={!!s.paper.auto_execute} onChange={(e) => set("paper", "auto_execute", e.target.checked)} className="mr-2" />
              Auto-execute executable signals as paper trades (real orders are never placed)
            </label>
            <div className="flex gap-4 text-sm">
              {["NIFTY", "SENSEX", "BANKNIFTY"].map((sym) => (
                <label key={sym}>
                  <input
                    type="checkbox"
                    className="mr-1"
                    checked={s.paper.symbols.includes(sym)}
                    onChange={(e) =>
                      set("paper", "symbols", e.target.checked ? [...s.paper.symbols, sym] : s.paper.symbols.filter((x: string) => x !== sym))
                    }
                  />
                  {sym}
                </label>
              ))}
            </div>
          </Card>
        </div>
        {Object.entries(s.strategies).map(([key, cfg]: any) => (
          <Card key={key} title={`${cfg.name} (${key})`}>
            <div className="grid grid-cols-2 gap-3">
              {Object.keys(cfg)
                .filter((k) => k !== "name")
                .map((k) => field("strategies", k, k.replace(/_/g, " "), typeof cfg[k] === "number" ? "number" : "text", key))}
            </div>
          </Card>
        ))}
      </div>
    </>
  );
};
