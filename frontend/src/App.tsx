import { useEffect } from "react";
import { BrowserRouter, Routes, Route, useNavigate } from "react-router-dom";
import { connectSocket, disconnectSocket } from "./services/socket";
import { useTraderStore } from "./store/useTraderStore";
import { Navbar } from "./components/layout";
import { Dashboard } from "./pages/Dashboard";
import { SettingsPage } from "./pages/Settings";
import { SignalsPage } from "./pages/Signals";
import { PositionsPage } from "./pages/Positions";

function AppWithSocket() {
  const { setState } = useTraderStore();

  useEffect(() => {
    const sock = connectSocket((msg) => {
      if (msg.event === "state") setState(msg.data);
    });
    return () => disconnectSocket();
  }, [setState]);

  return null;
}

export default function App() {
  return (
    <BrowserRouter>
      <AppWithSocket />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/signals" element={<SignalsPage />} />
        <Route path="/positions" element={<PositionsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Routes>
    </BrowserRouter>
  );
}
