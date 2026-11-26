import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.BACKEND_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // the ngrok tunnel's host names (tunnel.ps1); Vite refuses any other host but localhost
    allowedHosts: [".ngrok-free.app", ".ngrok-free.dev", ".ngrok.app", ".ngrok.dev"],
    proxy: {
      "/api": backend,
      "/ws": { target: backend, ws: true },
    },
  },
});
