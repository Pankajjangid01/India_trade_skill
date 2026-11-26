import { useState } from "react";
import { Play, Square } from "lucide-react";
import { api } from "../services/api";
import { useTraderStore } from "../store/useTraderStore";

// The button shows the backend's state only: it changes after the backend confirms, never optimistically.
export const EngineControl = () => {
  const { snap, socket, setSnap, notify } = useTraderStore();
  const [pending, setPending] = useState<"start" | "stop" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const running = !!snap?.engine.running;

  const act = async () => {
    const what = running ? "stop" : "start";
    setPending(what);
    setError(null);
    try {
      const s = await (what === "start" ? api.start() : api.stop());
      setSnap(s);
      if (s.engine.running !== (what === "start")) throw new Error(`backend reports the engine is ${s.engine.running ? "running" : "stopped"}`);
      notify("system", what === "start" ? "Engine started" : "Engine stopped", "Confirmed by the backend");
    } catch (e: any) {
      setError(`${what === "start" ? "Start" : "Stop"} failed: ${e.message}`);
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="flex flex-col items-end">
      <button
        onClick={act}
        disabled={!!pending || socket !== "connected" || !snap}
        className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-white disabled:opacity-50 disabled:cursor-not-allowed ${
          running ? "bg-red-600 hover:bg-red-700" : "bg-indian-green hover:bg-green-700"
        }`}
      >
        {running ? <Square className="w-4 h-4" /> : <Play className="w-4 h-4" />}
        {pending === "start" ? "Starting…" : pending === "stop" ? "Stopping…" : running ? "Stop engine" : "Start engine"}
      </button>
      {error && <p className="text-xs text-red-600 mt-1 max-w-xs text-right">{error}</p>}
    </div>
  );
};
