import { FormEvent, useEffect, useState } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { connectSocket, reconnectSocket } from "./services/socket";
import { api, token } from "./services/api";
import { useTraderStore } from "./store/useTraderStore";
import { Layout } from "./components/layout";
import { Dashboard } from "./pages/Dashboard";
import { SettingsPage } from "./pages/Settings";
import { SignalsPage } from "./pages/Signals";
import { PositionsPage } from "./pages/Positions";
import { BacktestPage } from "./pages/Backtest";
import { SkillsPage } from "./pages/Skills";

const loadState = () =>
  api.state().then(useTraderStore.getState().setSnap).catch(() => useTraderStore.getState().setSocket("disconnected"));

// Shown when the backend refuses the token: nothing else renders until a valid one is entered.
function TokenGate() {
  const [value, setValue] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    token.set(value);
    useTraderStore.getState().setLocked(false);
    reconnectSocket();
    loadState();
  };
  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm bg-white border border-slate-200 rounded-xl p-6 space-y-4">
        <div>
          <h1 className="font-bold text-slate-900 text-lg">Indian Day Trader</h1>
          <p className="text-sm text-slate-500">
            {token.get() ? "That access token was refused." : "Enter the access token."} It is the backend's APP_TOKEN
            and stays in this browser only.
          </p>
        </div>
        <input
          type="password"
          autoComplete="current-password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label="Access token"
          placeholder="Access token"
          className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm"
        />
        <button type="submit" className="w-full py-2 rounded-lg bg-slate-900 text-white text-sm font-medium">
          Unlock
        </button>
      </form>
    </div>
  );
}

export default function App() {
  const locked = useTraderStore((s) => s.locked);
  useEffect(() => {
    loadState();
    connectSocket();
  }, []);

  if (locked) return <TokenGate />;
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/signals" element={<SignalsPage />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/backtest" element={<BacktestPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
