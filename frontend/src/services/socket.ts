import { io, Socket } from "socket.io-client";
import { useTraderStore } from "../store/useTraderStore";
import { BACKEND, token } from "./api";

let socket: Socket | null = null;

// One connection for the whole app, reconnecting forever. The token goes in the handshake, not the URL.
export function connectSocket() {
  if (socket) return socket;
  const s = useTraderStore.getState();
  socket = io(BACKEND || undefined, {
    path: "/ws",
    auth: (cb) => cb({ token: token.get() }),
    transports: ["websocket", "polling"],
    reconnection: true,
    reconnectionDelayMax: 5000,
  });
  socket.on("connect", () => {
    s.setSocket("connected");
    s.setLocked(false);
  });
  socket.on("disconnect", () => s.setSocket("disconnected"));
  socket.on("connect_error", (err) => {
    s.setSocket("disconnected");
    if (err.message === "unauthorized") s.setLocked(true); // refused: Socket.IO does not retry this on its own
  });
  socket.on("state", (snap) => useTraderStore.getState().setSnap(snap));
  socket.on("signal", (x) =>
    s.notify("signal", `${x.strategy} ${x.symbol} ${x.direction}`, `${x.status}${x.reason ? ": " + x.reason : ""}`),
  );
  socket.on("trade_opened", (t) =>
    s.notify("fill", `Paper entry ${t.id}`, `${t.strategy} ${t.tradingsymbol} x${t.quantity} @ ${t.entry_price}`),
  );
  socket.on("trade_closed", (t) =>
    s.notify("exit", `Paper exit ${t.id}`, `${t.exit_reason} @ ${t.exit_price}, net ₹${t.net_pnl}`),
  );
  return socket;
}

// After a new token: connect again with it.
export function reconnectSocket() {
  socket?.disconnect().connect();
}
