import { resolve } from "node:path";

import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "dist/assets",
    emptyOutDir: false,
    rollupOptions: {
      input: resolve(import.meta.dirname, "src/legacy.css"),
      output: {
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css") === true
            ? "legacy.css"
            : "legacy-[name][extname]",
      },
    },
  },
});
