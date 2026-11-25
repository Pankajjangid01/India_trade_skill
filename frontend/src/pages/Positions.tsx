import { useTraderStore } from "../store/useTraderStore";
import { Navbar, Sidebar } from "../components/layout";
import { PositionCard } from "../components/SignalCard";

export const PositionsPage = () => {
  const { positions, dailyPnl, consecutiveLosses } = useTraderStore();

  const activePositions = positions.filter((p) => p.status === "PAPER_ACTIVE");
  const closedPositions = positions.filter((p) => p.status === "CLOSED");

  return (
    <div className="min-h-screen bg-slate-50">
      <Navbar />
      <div className="flex">
        <Sidebar active="positions" />
        <div className="flex-1 ml-64 pt-16 pb-8">
          <div className="max-w-6xl mx-auto px-6">
            <h1 className="text-2xl font-bold text-slate-900 mb-6">Positions</h1>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">
              <div className="bg-white rounded-xl border border-slate-200 p-5">
                <p className="text-xs text-slate-500 font-medium">Total Positions</p>
                <p className="text-2xl font-bold text-slate-900">{positions.length}</p>
              </div>
              <div className="bg-white rounded-xl border border-slate-200 p-5">
                <p className="text-xs text-slate-500 font-medium">Active</p>
                <p className="text-2xl font-bold text-green-600">{activePositions.length}</p>
              </div>
              <div className="bg-white rounded-xl border border-slate-200 p-5">
                <p className="text-xs text-slate-500 font-medium">Closed Today</p>
                <p className="text-2xl font-bold text-slate-900">{closedPositions.length}</p>
              </div>
            </div>

            <div className="bg-white rounded-xl border border-slate-200 p-6">
              <h2 className="text-lg font-bold text-slate-900 mb-4">Active Positions</h2>
              {activePositions.length > 0 ? (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  {activePositions.map((pos) => (
                    <PositionCard key={pos.id} pos={pos} />
                  ))}
                </div>
              ) : (
                <div className="text-center py-20">
                  <p className="text-slate-500">No active positions.</p>
                </div>
              )}
            </div>

            {closedPositions.length > 0 && (
              <div className="bg-white rounded-xl border border-slate-200 p-6 mt-6">
                <h2 className="text-lg font-bold text-slate-900 mb-4">Closed Positions Today</h2>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead>
                      <tr className="border-b border-slate-200">
                        <th className="pb-3 text-xs font-semibold text-slate-500">Symbol</th>
                        <th className="pb-3 text-xs font-semibold text-slate-500">Type</th>
                        <th className="pb-3 text-xs font-semibold text-slate-500">Entry</th>
                        <th className="pb-3 text-xs font-semibold text-slate-500">Exit</th>
                        <th className="pb-3 text-xs font-semibold text-slate-500">P&L</th>
                        <th className="pb-3 text-xs font-semibold text-slate-500">Reason</th>
                        <th className="pb-3 text-xs font-semibold text-slate-500">Time</th>
                      </tr>
                    </thead>
                    <tbody>
                      {closedPositions.map((pos) => (
                        <tr key={pos.id} className="border-b border-slate-50 last:border-0">
                          <td className="py-3 text-slate-900 font-medium">{pos.symbol}</td>
                          <td className="py-3 text-slate-600">{pos.direction}</td>
                          <td className="py-3 text-slate-600">{pos.entry_price?.toLocaleString("en-IN")}</td>
                          <td className="py-3 text-slate-600">{pos.exit_price?.toLocaleString("en-IN")}</td>
                          <td className={`py-3 font-medium ${pos.pnl >= 0 ? "text-green-600" : "text-red-600"}`}>
                            {pos.pnl >= 0 ? "+" : ""}{pos.pnl.toLocaleString("en-IN")}
                          </td>
                          <td className="py-3 text-slate-600">{pos.exit_reason}</td>
                          <td className="py-3 text-slate-400 text-xs">{pos.exit_time}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="mt-4 pt-4 border-t border-slate-200">
                  <p className="text-sm font-medium text-slate-900">Total Realised P&L: </p>
                  <p className={`text-xl font-bold ${dailyPnl >= 0 ? "text-green-600" : "text-red-600"}`}>
                    ₹{dailyPnl.toLocaleString("en-IN")}
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
