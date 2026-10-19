import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API/WebSocket to the FastAPI backend on 8321.
// Production builds are served by FastAPI itself, so all URLs stay relative.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8321",
      "/ws": { target: "ws://127.0.0.1:8321", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
