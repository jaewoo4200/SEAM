import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The app always calls relative "/api/..." URLs; in dev Vite proxies them to
// the FastAPI backend so there is no CORS boundary.
export default defineConfig({
  plugins: [react()],
  server: {
    // Honor a harness/CI-assigned port (PORT env). Default stays 5173, but
    // that port often belongs to ANOTHER local app — CLI --port (e.g. the
    // 4610 preview config) still takes precedence over both.
    port: process.env.PORT ? Number(process.env.PORT) : 5173,
    proxy: {
      // 127.0.0.1, not localhost: on Windows Node resolves localhost to ::1
      // first while uvicorn binds the IPv4 loopback, breaking the proxy.
      "/api": "http://127.0.0.1:8000",
      // The live-event WebSocket lives under /ws; without this the "Simulation
      // finished" push never arrives in dev (silent, easy to miss). ws:true
      // upgrades the proxied connection to a WebSocket.
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
