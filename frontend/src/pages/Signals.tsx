import { useEffect, useState } from "react";
import { api } from "../services/api";
import { useTraderStore, type Signal } from "../store/useTraderStore";
import { SignalsTable } from "../components/tables";
import { Card, todayIST } from "../components/ui";

export const SignalsPage = () => {
  const { snap } = useTraderStore();
  const [date, setDate] = useState(todayIST());
  const [past, setPast] = useState<Signal[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [strategy, setStrategy] = useState("ALL");
  const [status, setStatus] = useState("ALL");
  const isToday = snap ? date === snap.today : true;

  useEffect(() => {
    if (isToday) return setPast(null);
    setErr(null);
    api.signals(date).then(setPast).catch((e) => setErr(e.message));
  }, [date, isToday]);

  const rows = (isToday ? snap?.signals || [] : past || []).filter(
    (s) => (strategy === "ALL" || s.strategy === strategy) && (status === "ALL" || s.status === status),
  );
  const sel = "border border-slate-300 rounded-md px-2 py-1 text-sm bg-white";
  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900 mb-5">Signals</h1>
      <Card
        title={`${rows.length} signal(s) on ${date}${isToday ? " (live)" : ""}`}
        right={
          <div className="flex gap-2">
            <input type="date" value={date} onChange={(e) => setDate(e.target.value)} className={sel} />
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)} className={sel}>
              <option value="ALL">All strategies</option>
              <option value="ORB">ORB</option>
              <option value="EMA_PULLBACK">EMA Pullback</option>
            </select>
            <select value={status} onChange={(e) => setStatus(e.target.value)} className={sel}>
              <option value="ALL">All statuses</option>
              <option value="EXECUTED">Executed</option>
              <option value="EXECUTABLE">Executable</option>
              <option value="ENTRY_CUTOFF">Entry Cutoff</option>
              <option value="REJECTED">Rejected</option>
            </select>
          </div>
        }
      >
        {err && <p className="text-sm text-red-600 mb-2">{err}</p>}
        <SignalsTable rows={rows} empty="No signals for this selection." />
        <p className="text-xs text-slate-400 mt-3">
          Executed: a paper trade was opened. Executable: valid, but auto-execute was off. Entry Cutoff: generated after the
          entry cutoff, recorded only. Rejected: a risk rule, a stale bar, a conflicting position or the option pick said no.
        </p>
      </Card>
    </>
  );
};
