import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export interface Position {
  id: string;
  symbol: string;
  instrument: string;
  token: string;
  direction: "bullish" | "bearish";
  entry_price: number;
  stop_loss: number;
  target: number;
  underlying_stop: number;
  underlying_target: number;
  lot_size: number;
  quantity: number;
  status: string;
  entry_time: string;
  pnl: number;
  exit_reason: string | null;
  exit_time: string | null;
  exit_price: number | null;
  risk_r: number;
  r_value: number;
}

export interface Order {
  id: string;
  symbol: string;
  side: string;
  price: number;
  quantity: number;
  status: string;
  timestamp: string;
}

export interface Signal {
  symbol: string;
  status: string;
  direction: string | null;
  trigger_level: number | null;
  bar_close: number | null;
  bar_close_ist: string | null;
  timestamp: string;
  pick?: any;
  plan?: any;
  budget?: any;
  risk?: any;
}

export interface Settings {
  capital_inr: number;
  risk_per_trade_pct: number;
  max_daily_loss_pct: number;
  max_trades_per_day: number;
  max_consecutive_losses: number;
  max_capital_per_trade_pct: number;
  entry_start: string;
  entry_end: string;
  flat_by: string;
  expiry_day_flat_by: string;
  stop_rule: string;
  target_r: number;
  max_premium_loss_pct: number;
  no_progress_bars: number;
  paper_trading_enabled: boolean;
  auto_execute: boolean;
  symbols: string[];
}

interface TraderState {
  // Connection
  connected: boolean;
  marketHours: boolean;
  running: boolean;
  // Data
  positions: Position[];
  orders: Order[];
  dailyPnl: number;
  consecutiveLosses: number;
  lastSignal: Signal | null;
  lastScan: any;
  settings: Settings;
  timestamp: string;
  // Notifications
  notifications: Notification[];
  // Actions
  setConnected: (v: boolean) => void;
  setMarketHours: (v: boolean) => void;
  setRunning: (v: boolean) => void;
  setState: (state: any) => void;
  updatePosition: (pos: Position) => void;
  closePosition: (id: string, reason: string, price: number) => void;
  addNotification: (n: Omit<Notification, "id">) => void;
  removeNotification: (id: string) => void;
  updateSettings: (s: Partial<Settings>) => void;
}

export interface Notification {
  id: string;
  type: "signal" | "alert" | "fill" | "exit" | "system";
  title: string;
  body: string;
  timestamp: string;
  read: boolean;
}

const defaultSettings: Settings = {
  capital_inr: 500000,
  risk_per_trade_pct: 1.0,
  max_daily_loss_pct: 2.0,
  max_trades_per_day: 3,
  max_consecutive_losses: 3,
  max_capital_per_trade_pct: 30,
  entry_start: "09:30",
    entry_end: "14:00",
  flat_by: "15:10",
  expiry_day_flat_by: "14:45",
  stop_rule: "or-mid",
  target_r: 2.0,
  max_premium_loss_pct: 30,
  no_progress_bars: 6,
  paper_trading_enabled: true,
  auto_execute: true,
  symbols: ["NIFTY", "SENSEX", "BANKNIFTY"],
};

export const useTraderStore = create<TraderState>()(
  persist(
    (set, get) => ({
      connected: false,
      marketHours: false,
      running: false,
      positions: [],
      orders: [],
      dailyPnl: 0,
      consecutiveLosses: 0,
      lastSignal: null,
      lastScan: null,
      settings: defaultSettings,
      timestamp: "",
      notifications: [],

      setConnected: (v) => set({ connected: v }),
      setMarketHours: (v) => set({ marketHours: v }),
      setRunning: (v) => set({ running: v }),

      setState: (state) => {
        set({
          positions: state.positions || [],
          orders: state.orders || [],
          dailyPnl: state.daily_pnl ?? 0,
          consecutiveLosses: state.consecutive_losses ?? 0,
          lastSignal: state.last_signal || null,
          lastScan: state.last_scan || null,
          settings: state.settings || defaultSettings,
          timestamp: state.timestamp || "",
          marketHours: state.is_market_hours ?? false,
          running: state.running ?? false,
        });
      },

      updatePosition: (pos) =>
        set((s) => ({
          positions: s.positions.map((p) => (p.id === pos.id ? { ...p, ...pos } : p)),
        })),

      closePosition: (id, reason, price) =>
        set((s) => ({
          positions: s.positions.map((p) =>
            p.id === id ? { ...p, status: "CLOSED", exit_reason: reason, exit_price: price, exit_time: new Date().toISOString() } : p
          ),
        })),

      addNotification: (n) =>
        set((s) => {
          const id = Date.now().toString();
          const notification = { ...n, id, read: false, timestamp: new Date().toISOString() };
          return { notifications: [notification, ...s.notifications].slice(0, 50) };
        }),

      removeNotification: (id) =>
        set((s) => ({ notifications: s.notifications.filter((n) => n.id !== id) })),

      updateSettings: (s) =>
        set((state) => ({ settings: { ...state.settings, ...s } })),
    }),
    {
      name: "trader-storage",
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ settings: s.settings }),
    },
  ),
);
