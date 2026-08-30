import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard is served by the existing Flask routes: `/` returns the
// built index.html and every asset rides Flask's /static/ route. The build
// therefore lands inside the Flask package's static dir under app/, with
// hashed asset names so the 1-hour static cache policy can never hide a
// deploy (the no-store page always names the current hash).
export default defineConfig({
  plugins: [react()],
  base: "/static/app/",
  build: {
    outDir: "../src/codex_usage_tracker/static/app",
    emptyOutDir: true,
    // Readable (unminified) CSS keeps the committed bundle diffable and lets
    // the theme-conformance purity scan read the built stylesheet directly.
    cssMinify: false,
    rollupOptions: {
      output: {
        // App code and third-party code split so the conformance scan can
        // hold the APP chunk to the zero-color-literal law (React internals
        // are outside that law, like the vendored GC snapshot).
        manualChunks: (id) => (id.includes("node_modules") ? "vendor" : undefined),
      },
    },
  },
  server: {
    // Dev mode: `npm run dev` proxies API + static (GC loader, Chart.js
    // vendor) to a locally running Flask dashboard.
    proxy: {
      "/api": "http://127.0.0.1:5174",
      "/static": "http://127.0.0.1:5174",
    },
  },
});
