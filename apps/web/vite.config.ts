import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The control plane serves the built SPA under /app, so assets must resolve
// from that base in production while staying at root for the dev server.
const base = process.env.WEB_BASE ?? "/app/";

// Proxy the API to the running control plane during development so the SPA
// talks to real endpoints without CORS setup.
const apiTarget = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig(({ command }) => ({
  base: command === "build" ? base : "/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
}));
