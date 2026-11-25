import { io, Socket } from "socket.io-client";

let socket: Socket | null = null;

export const connectSocket = (onMessage: (data: any) => void) => {
  if (socket) return socket;
  socket = io("http://localhost:8000", {
    path: "/ws",
    transports: ["websocket", "polling"],
    autoConnect: true,
    reconnection: true,
    reconnectionAttempts: 5,
    reconnectionDelay: 1000,
  });
  socket.on("connect", () => {
    onMessage({ event: "connect", data: null });
  });
  socket.on("disconnect", () => {
    onMessage({ event: "disconnect", data: null });
  });
  socket.on("connect_error", (err: any) => {
    console.error("Socket connect error:", err.message);
    onMessage({ event: "connect_error", data: err.message });
  });
  socket.on("state", (data: any) => onMessage({ event: "state", data }));
  socket.on("signal_update", (data: any) => onMessage({ event: "signal_update", data }));
  socket.on("scan_update", (data: any) => onMessage({ event: "scan_update", data }));
  socket.on("news_update", (data: any) => onMessage({ event: "news_update", data }));
  socket.on("trigger_detected", (data: any) => onMessage({ event: "trigger_detected", data }));
  socket.on("paper_trade_executed", (data: any) => onMessage({ event: "paper_trade_executed", data }));
  socket.on("position_closed", (data: any) => onMessage({ event: "position_closed", data }));
  return socket;
};

export const disconnectSocket = () => {
  if (socket) {
    socket.disconnect();
    socket = null;
  }
};

export { socket };
