import { create } from "zustand";

// The UI keeps no trading state of its own: `snap` is the backend's snapshot, replaced on every push.

export interface StrategyStats {
  signals: number;
  executable: number;
  entry_cutoff: number;
  rejected: number;
  trades: number;
  open: number;
  completed: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  net_pnl: number;
  gross_pnl: number;
  charges: number;
  best: number | null;
  worst: number | null;
}

export interface Signal {
  id: string;
  date: string;
  strategy: string;
  symbol: string;
  direction: "bullish" | "bearish";
  bar_start_ist: string;
  bar_close_ist: string;
  detected_at: string;
  entry_underlying: number;
  stop_underlying: number;
  target_underlying: number;
  risk_points: number;
  note: string | null;
  status: "EXECUTED" | "EXECUTABLE" | "ENTRY_CUTOFF" | "REJECTED";
  reason: string | null;
  trade_id: string | null;
  meta: any;
}

export interface Trade {
  id: string;
  signal_id: string;
  date: string;
  strategy: string;
  symbol: string;
  direction: "bullish" | "bearish";
  tradingsymbol: string;
  segment: string;
  lots: number;
  quantity: number;
  entry_price: number;
  entry_time: string;
  entry_underlying: number;
  stop_premium: number;
  target_premium: number;
  stop_underlying: number;
  target_underlying: number;
  flat_by: string;
  status: "OPEN" | "CLOSED";
  ltp: number | null;
  ltp_time: string | null;
  underlying_ltp: number | null;
  exit_price: number | null;
  exit_time: string | null;
  exit_reason: string | null;
  gross_pnl: number | null;
  charges: number | null;
  net_pnl: number | null;
  unrealized_net?: number;
  unrealized_gross?: number;
}

export interface Snapshot {
  server_time_ist: string;
  today: string;
  engine: {
    running: boolean;
    desired_running: boolean;
    phase: string | null;
    phase_reason: string | null;
    last_cycle_ist: string | null;
    last_error: string | null;
    errors: number;
    started_at: string | null;
    cycles: number;
  };
  angel: {
    logged_in: boolean;
    session_date: string | null;
    last_ok_ist: string | null;
    last_error: string | null;
    feed: {
      alive: boolean;
      managed: boolean;
      connected: boolean;
      reconnects: number;
      ticks: number;
      subscribed: number | null;
      lag_median_ms: number | null;
      lag_worst_ms: number | null;
      stale: number;
      last_error: string | null;
      updated_ist: string | null;
      restarts: number;
    };
  };
  market: {
    symbol: string;
    ltp: number | null;
    source: string | null;
    age_sec: number | null;
    stale: boolean;
    prev_close: number | null;
    change: number | null;
    change_pct: number | null;
    expiry_today: boolean;
    bars_source?: string | null;
    bars?: number;
    last_bar_ist?: string | null;
  }[];
  config: {
    entry_start: string;
    entry_cutoff: string;
    flat_by: string;
    auto_execute: boolean;
    symbols: string[];
    capital_inr: number | null;
  };
  strategies: { key: string; name: string; enabled: boolean; [k: string]: any }[];
  signals: Signal[];
  open_trades: Trade[];
  closed_trades: Trade[];
  stats_today: Record<string, StrategyStats>;
  stats_all_time: Record<string, StrategyStats>;
  unrealized_net: number;
}

export interface Notification {
  id: number;
  type: "signal" | "fill" | "exit" | "system";
  title: string;
  body: string;
  at: number;
  read: boolean;
}

interface TraderState {
  snap: Snapshot | null;
  receivedAt: number;
  socket: "connecting" | "connected" | "disconnected";
  notifications: Notification[];
  locked: boolean; // the backend refused our access token (or we have none)
  setSnap: (s: Snapshot) => void;
  setSocket: (s: TraderState["socket"]) => void;
  setLocked: (locked: boolean) => void;
  notify: (type: Notification["type"], title: string, body: string) => void;
  markAllRead: () => void;
}

let nid = 0;
export const useTraderStore = create<TraderState>()((set) => ({
  snap: null,
  receivedAt: 0,
  socket: "connecting",
  notifications: [],
  locked: false,
  setSnap: (snap) => set({ snap, receivedAt: Date.now() }),
  setSocket: (socket) => set({ socket }),
  setLocked: (locked) => set({ locked }),
  notify: (type, title, body) =>
    set((s) => ({
      notifications: [{ id: ++nid, type, title, body, at: Date.now(), read: false }, ...s.notifications].slice(0, 50),
    })),
  markAllRead: () => set((s) => ({ notifications: s.notifications.map((n) => ({ ...n, read: true })) })),
}));
