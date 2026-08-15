import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const repositoryRoot = fileURLToPath(new URL("../", import.meta.url));

export default defineConfig({
  base: "/mail/",
  plugins: [react()],
  build: {
    manifest: true,
    rollupOptions: {
      output: {
        // React, react-dom and scheduler are MIT, and MIT requires their
        // copyright notice to travel with any redistribution. The default build
        // dropped every one of them: `grep -rl '@license' dist/` found nothing
        // while node_modules/react/cjs/react.production.js still opened with
        // `@license React`. This bundle is not a private artifact — the
        // Dockerfile copies it into the published image and the server hands it
        // to browsers — so the stripping was happening on a distribution path.
        //
        // The knob is here rather than under `esbuild`: Vite 8 minifies with
        // oxc, not esbuild, and defaults this output to `legal: !minify`, so a
        // production build discards legal comments by construction. Only the
        // legal class is turned back on — annotation and jsdoc comments stay
        // stripped, so the bundle keeps its size and loses only the omission
        // that was a licence problem.
        comments: { legal: true },
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: [
        "text",
        "json-summary",
        ["lcov", { projectRoot: repositoryRoot }],
      ],
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/legacy.ts",
        "src/main.tsx",
        "src/vite-env.d.ts",
        "src/test/**",
        "src/**/*.test.{ts,tsx}",
      ],
      thresholds: {
        branches: 100,
        functions: 100,
        lines: 100,
        statements: 100,
      },
    },
  },
});
