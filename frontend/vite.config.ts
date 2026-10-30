import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the API runs separately (`python -m app.api`, port 8000); Vite proxies /api to it.
// `npm run build` writes dist/, which the API serves at / in a single-process deployment.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.VITE_API_URL ?? "http://127.0.0.1:8000", changeOrigin: true } },
  },
  test: { environment: "node" },
});
