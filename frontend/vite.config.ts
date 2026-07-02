import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The app always calls relative "/api/..." URLs; in dev Vite proxies them to
// the FastAPI backend so there is no CORS boundary.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
