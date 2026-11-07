import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development Vite serves the page and forwards the API, so that everything stays on one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:7321" },
  },
  build: { sourcemap: false, chunkSizeWarningLimit: 900 },
  test: { include: ["src/**/*.test.ts"] },
});
