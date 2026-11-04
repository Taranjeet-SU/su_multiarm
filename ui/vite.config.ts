/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The bundle is served by the package's HTTP server under its mount (default /ui), so asset URLs are
// relative ("./assets/..."); the build never inlines scripts or styles (strict CSP: script-src 'self',
// style-src 'self'); the output lands in the Python wrapper package (gitignored, built in CI).
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "python/su_multi_arm_ui/static",
    emptyOutDir: true,
    assetsInlineLimit: 0,
    sourcemap: false,
    modulePreload: { polyfill: false },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
