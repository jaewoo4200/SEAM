import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The app always calls relative "/api/..." URLs; in dev Vite proxies them to
// the FastAPI backend so there is no CORS boundary.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // 127.0.0.1, not localhost: on Windows Node resolves localhost to ::1
      // first while uvicorn binds the IPv4 loopback, breaking the proxy.
      "/api": "http://127.0.0.1:8000",
    },
  },
});
