import { Save, RefreshCw, ToggleLeft, ToggleRight } from "lucide-react";

export const SettingsForm = ({
  settings,
  onUpdate,
  onSave,
}: {
  settings: any;
  onUpdate: (key: string, value: any) => void;
  onSave: () => void;
}) => {
  const toggleBool = (key: string) => onUpdate(key, !settings[key]);

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-6">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-lg font-bold text-slate-900">Trading Settings</h2>
        <button
          onClick={onSave}
          className="flex items-center gap-2 bg-indian-green text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-green-700 transition"
        >
          <Save className="w-4 h-4" /> Save Settings
        </button>
      </div>

      <div className="space-y-6">
        {/* Capital & Risk */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="bg-slate-50 rounded-lg p-4 border border-slate-100">
            <h3 className="text-xs font-semibold text-slate-500 uppercase mb-3">Capital & Risk</h3>
            <div className="space-y-3">
              <div>
                <label className="text-sm text-slate-700 block mb-1">Capital (₹)</label>
                <input
                  type="number"
                  value={settings.capital_inr}
                  onChange={(e) => onUpdate("capital_inr", Number(e.target.value))}
                  className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
                />
              </div>
              <div>
                <label className="text-sm text-slate-700 block mb-1">Risk Per Trade (%)</label>
                <input
                  type="number"
                  step="0.1"
                  value={settings.risk_per_trade_pct}
                  onChange={(e) => onUpdate("risk_per_trade_pct", Number(e.target.value))}
                  className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
                />
              </div>
              <div>
                <label className="text-sm text-slate-700 block mb-1">Max Daily Loss (%)</label>
                <input
                  type="number"
                  step="0.1"
                  value={settings.max_daily_loss_pct}
                  onChange={(e) => onUpdate("max_daily_loss_pct", Number(e.target.value))}
                  className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
                />
              </div>
            </div>
          </div>

          <div className="bg-slate-50 rounded-lg p-4 border border-slate-100">
            <h3 className="text-xs font-semibold text-slate-500 uppercase mb-3">Limits</h3>
            <div className="space-y-3">
              <div>
                <label className="text-sm text-slate-700 block mb-1">Max Trades Per Day</label>
                <input
                  type="number"
                  value={settings.max_trades_per_day}
                  onChange={(e) => onUpdate("max_trades_per_day", Number(e.target.value))}
                  className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
                />
              </div>
              <div>
                <label className="text-sm text-slate-700 block mb-1">Max Consecutive Losses</label>
                <input
                  type="number"
                  value={settings.max_consecutive_losses}
                  onChange={(e) => onUpdate("max_consecutive_losses", Number(e.target.value))}
                  className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
                />
              </div>
              <div>
                <label className="text-sm text-slate-700 block mb-1">Max Capital Per Trade (%)</label>
                <input
                  type="number"
                  step="0.1"
                  value={settings.max_capital_per_trade_pct}
                  onChange={(e) => onUpdate("max_capital_per_trade_pct", Number(e.target.value))}
                  className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
                />
              </div>
            </div>
          </div>
        </div>

        {/* Timing */}
        <div className="bg-slate-50 rounded-lg p-4 border border-slate-100">
          <h3 className="text-xs font-semibold text-slate-500 uppercase mb-3">Session Timing (IST)</h3>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div>
              <label className="text-sm text-slate-700 block mb-1">Entry Start</label>
              <input
                value={settings.entry_start}
                onChange={(e) => onUpdate("entry_start", e.target.value)}
                className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
              />
            </div>
            <div>
              <label className="text-sm text-slate-700 block mb-1">Entry End</label>
              <input
                value={settings.entry_end}
                onChange={(e) => onUpdate("entry_end", e.target.value)}
                className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
              />
            </div>
            <div>
              <label className="text-sm text-slate-700 block mb-1">Flat By</label>
              <input
                value={settings.flat_by}
                onChange={(e) => onUpdate("flat_by", e.target.value)}
                className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
              />
            </div>
            <div>
              <label className="text-sm text-slate-700 block mb-1">Expiry Flat By</label>
              <input
                value={settings.expiry_day_flat_by}
                onChange={(e) => onUpdate("expiry_day_flat_by", e.target.value)}
                className="w-full px-3 py-2 bg-white border border-slate-300 rounded-md text-sm focus:ring-2 focus:ring-indian-green focus:outline-none"
              />
            </div>
          </div>
        </div>

        {/* Toggles */}
        <div className="bg-slate-50 rounded-lg p-4 border border-slate-100">
          <h3 className="text-xs font-semibold text-slate-500 uppercase mb-3">Auto Trading</h3>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm text-slate-700">Auto Execute Trades</span>
              <button
                onClick={() => toggleBool("auto_execute")}
                className={`relative w-12 h-6 rounded-full ${settings.auto_execute ? "bg-indian-green" : "bg-slate-300"} transition`}
              >
                {settings.auto_execute ? (
                  <ToggleRight className="w-4 h-4 text-white absolute right-[2px] top-[1px]" />
                ) : (
                  <ToggleLeft className="w-4 h-4 text-white absolute left-[2px] top-[1px]" />
                )}
              </button>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-slate-700">Paper Trading Mode</span>
              <button
                onClick={() => toggleBool("paper_trading_enabled")}
                className={`relative w-12 h-6 rounded-full ${settings.paper_trading_enabled ? "bg-indian-green" : "bg-slate-300"} transition`}
              >
                {settings.paper_trading_enabled ? (
                  <ToggleRight className="w-4 h-4 text-white absolute right-[2px] top-[1px]" />
                ) : (
                  <ToggleLeft className="w-4 h-4 text-white absolute left-[2px] top-[1px]" />
                )}
              </button>
            </div>
          </div>
        </div>

        <button
          onClick={onSave}
          className="w-full bg-slate-900 text-white py-3 rounded-lg text-sm font-medium hover:bg-slate-800 transition flex items-center justify-center gap-2"
        >
          <RefreshCw className="w-4 h-4" /> Apply to Skill Pack
        </button>
      </div>
    </div>
  );
};
