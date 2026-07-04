import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy the FastAPI backend during `vite dev`. In production the built bundle
// is served by FastAPI itself, so these only apply to local development.
const backend = "http://localhost:8000";
const proxied = ["/auth", "/admin", "/a2a", "/chat", "/documents", "/npci-tickets", "/health"];

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    // Keep everything as external files so the strict CSP (no inline
    // script/style) holds: no data-URI assets, no inlined modulepreload polyfill.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      proxied.map((p) => [p, { target: backend, changeOrigin: false }]),
    ),
  },
});
