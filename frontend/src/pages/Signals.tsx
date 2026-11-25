import { useTraderStore } from "../store/useTraderStore";
import { Navbar, Sidebar } from "../components/layout";
import { SignalCard } from "../components/SignalCard";

export const SignalsPage = () => {
  const { lastSignal, lastScan } = useTraderStore();

  return (
    <div className="min-h-screen bg-slate-50">
      <Navbar />
      <div className="flex">
        <Sidebar active="signals" />
        <div className="flex-1 ml-64 pt-16 pb-8">
          <div className="max-w-4xl mx-auto px-6">
            <h1 className="text-2xl font-bold text-slate-900 mb-6">Live Signals</h1>

            <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6">
              <h2 className="text-lg font-bold text-slate-900 mb-3">Current Signal</h2>
              {lastSignal ? (
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-slate-900 uppercase">{lastSignal.symbol}</span>
                    <span className={`px-2 py-1 rounded text-xs font-medium ${
                      lastSignal.status === "triggered"
                        ? "bg-green-100 text-green-800 border-green-200"
                        : lastSignal.status === "waiting"
                          ? "bg-amber-100 text-amber-800 border-amber-200"
                          : "bg-slate-100 text-slate-600 border-slate-200"
                    }`}>
                      {lastSignal.status}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    <div>
                      <p className="text-xs text-slate-500">Direction</p>
                      <p className="font-medium text-slate-900 capitalize">
                        {lastSignal.direction || "Waiting"}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-500">Trigger Level</p>
                      <p className="font-medium text-slate-900">
                        {lastSignal.trigger_level?.toLocaleString("en-IN") || "—"}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-500">Bar Close</p>
                      <p className="font-medium text-slate-900">
                        {lastSignal.bar_close?.toLocaleString("en-IN") || "—"}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-500">Time</p>
                      <p className="font-medium text-slate-900">{lastSignal.timestamp || "—"}</p>
                    </div>
                  </div>
                  {lastSignal.pick && (
                    <div className="bg-slate-50 rounded-lg p-4 mt-4">
                      <p className="text-xs font-semibold text-slate-500 uppercase mb-2">Pick (in-chain)</p>
                      <p className="text-sm font-medium text-slate-900">
                        {lastSignal.pick.instrument?.tradingsymbol || "—"}
                      </p>
                      <p className="text-xs text-slate-500">
                        Premium: {lastSignal.pick.premium?.toLocaleString("en-IN")} | Delta: {lastSignal.pick.delta}
                      </p>
                    </div>
                  )}
                  {lastSignal.plan && (
                    <div className="bg-slate-50 rounded-lg p-4 mt-4">
                      <p className="text-xs font-semibold text-slate-500 uppercase mb-2">Exit Plan (in-exit)</p>
                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <p className="text-xs text-slate-400">Premium Stop</p>
                          <p className="text-sm font-medium text-slate-900">
                            {lastSignal.plan.premium_stop?.toLocaleString("en-IN")}
                          </p>
                        </div>
                        <div>
                          <p className="text-xs text-slate-400">Premium Target</p>
                          <p className="text-sm font-medium text-slate-900">
                            {lastSignal.plan.target_premium?.toLocaleString("en-IN")}
                          </p>
                        </div>
                        <div>
                          <p className="text-xs text-slate-400">Underlying Stop</p>
                          <p className="text-sm font-medium text-slate-900">
                            {lastSignal.plan.underlying_stop?.toLocaleString("en-IN")}
                          </p>
                        </div>
                        <div>
                          <p className="text-xs text-slate-400">Underlying Target</p>
                          <p className="text-sm font-medium text-slate-900">
                            {lastSignal.plan.target_underlying?.toLocaleString("en-IN")}
                          </p>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-center py-20">
                  <p className="text-slate-500">No signal yet. Waiting for ORB breakout...</p>
                </div>
              )}
            </div>

            {lastScan && (
              <div className="bg-white rounded-xl border border-slate-200 p-6">
                <h2 className="text-lg font-bold text-slate-900 mb-3">Last Scan (in-scanner)</h2>
                <pre className="bg-slate-50 rounded-lg p-4 text-xs overflow-x-auto">{JSON.stringify(lastScan, null, 2)}</pre>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
