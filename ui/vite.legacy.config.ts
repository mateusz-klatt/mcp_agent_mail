import { resolve } from "node:path";

import { defineConfig } from "vite";

export default defineConfig({
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: "dist/assets",
    emptyOutDir: false,
    lib: {
      entry: resolve(import.meta.dirname, "src/legacy.ts"),
      name: "HermesLegacy",
      formats: ["iife"],
      fileName: () => "legacy.js",
    },
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css") === true
            ? "legacy.css"
            : "legacy-[name][extname]",
      },
    },
  },
});
