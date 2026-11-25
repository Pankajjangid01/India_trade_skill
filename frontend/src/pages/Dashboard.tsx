import { useEffect, useState } from "react";
import { useTraderStore } from "../store/useTraderStore";
import { getState, startTrader, stopTrader } from "../services/api";
import { Navbar, Sidebar, StatCard, AlertBanner } from "../components/layout";
import { NotificationBell } from "../components/NotificationBell";
import { SignalCard, PositionCard } from "../components/SignalCard";

export const Dashboard = () => {
  const {
    connected,
    marketHours,
    running,
    positions,
    dailyPnl,
    consecutiveLosses,
    lastSignal,
    setState,
    setConnected,
    setMarketHours,
    setRunning,
    notifications,
    addNotification,
  } = useTraderStore();

  const [isLoading, setIsLoading] = useState(false);
  const [initialized, setInitialized] = useState(false);

  // Load state once on mount, then rely on socket for live updates
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await getState();
        if (!cancelled) {
          setState(res.data);
          setConnected(true);
          setInitialized(true);
        }
      } catch (e) {
        setConnected(false);
        setInitialized(true);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [setState, setConnected]);

  // Socket for live updates (single connection in App.tsx already handles this)
  useEffect(() => {
    // Socket is managed globally in App.tsx; local state updates come from store
  }, []);

  const handleStartStop = async () => {
    setIsLoading(true);
    const prevRunning = running;
    try {
      const nextRunning = !running;
      // Optimistic UI update
      setRunning(nextRunning);
      if (nextRunning) {
        await startTrader();
      } else {
        await stopTrader();
      }
      // Sync with server state
      const res = await getState();
      setState(res.data);
      setRunning(res.data.running);
      setMarketHours(res.data.is_market_hours);
    } catch (e) {
      // Revert optimistic update on failure
      setRunning(prevRunning);
      console.error("Start/Stop failed:", e);
      addNotification({
        type: "system",
        title: "❌ Action Failed",
        body: "Could not reach backend. Is the server running?",
      });
    } finally {
      setIsLoading(false);
    }
  };

  const activePositions = positions.filter((p) => p.status === "PAPER_ACTIVE");

  return (
    <div className="min-h-screen bg-slate-50">
      <Navbar />
      <div className="flex">
        <Sidebar active="dashboard" />
        <div className="flex-1 ml-64 pt-16 pb-8">
          <div className="max-w-7xl mx-auto px-6">
            <div className="flex items-center justify-between mb-6">
              <div>
                <h1 className="text-2xl font-bold text-slate-900">Dashboard</h1>
                <p className="text-slate-500 text-sm">
                  Live ORB Signals • Auto Paper Trading • {new Date().toLocaleTimeString("en-IN")}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={handleStartStop}
                  disabled={isLoading}
                  className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition ${
                    running
                      ? "bg-red-600 text-white hover:bg-red-700"
                      : "bg-indian-green text-white hover:bg-green-700"
                  } ${isLoading ? "opacity-50 cursor-not-allowed" : ""}`}
                >
                  {isLoading ? (running ? "Stopping..." : "Starting...") : running ? "Stop Auto Trader" : "Start Auto Trader"}
                </button>
                <NotificationBell notifications={notifications} onRemove={() => {}} />
              </div>
            </div>

            {!marketHours && (
              <AlertBanner type="warning" message="Outside market hours (09:00–15:30 IST). Auto-trader will not run." />
            )}
            {marketHours && !running && (
              <AlertBanner type="info" message="Market is open. Click Start Auto Trader to begin scanning." />
            )}

            <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
              <StatCard title="Daily P&L" value={`₹${dailyPnl.toLocaleString("en-IN")}`} sub="Realised today" trend={dailyPnl >= 0 ? "up" : "down"} />
              <StatCard title="Active Positions" value={activePositions.length} sub="Paper trading" trend="neutral" />
              <StatCard title="Consecutive Losses" value={consecutiveLosses} sub="Limit: 3" trend={consecutiveLosses >= 3 ? "down" : "neutral"} />
              <StatCard title="System" value={running ? "Live" : "Stopped"} sub={running ? "Auto-trading on" : "Idle"} trend={running ? "up" : "neutral"} />
            </div>

            <div className="mb-6">
              <h2 className="text-lg font-bold text-slate-900 mb-3">Live Signal</h2>
              {lastSignal ? (
                <SignalCard signal={lastSignal} />
              ) : (
                <div className="bg-white rounded-xl border border-slate-200 p-8 text-center">
                  <p className="text-slate-500">No signal yet. Waiting for ORB breakout...</p>
                </div>
              )}
            </div>

            <div className="mb-6">
              <h2 className="text-lg font-bold text-slate-900 mb-3">Positions</h2>
              {positions.length > 0 ? (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  {positions.map((pos) => (
                    <PositionCard key={pos.id} pos={pos} />
                  ))}
                </div>
              ) : (
                <div className="bg-white rounded-xl border border-slate-200 p-8 text-center">
                  <p className="text-slate-500">No positions yet.</p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
