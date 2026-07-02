import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Resolve paths from this config file's own location, not the shell's cwd,
// so `npm run build` behaves the same run from the repo root (the normal
// case, via `just frontend-build`) or anywhere else. Uses the global `URL`
// instead of `node:path`/`node:url` so the island doesn't need @types/node.
const here = new URL(".", import.meta.url).pathname;
const outDir = `${here}../../app/store_project/static/js/dist`;

// Builds the designer React island to STABLE, un-hashed filenames in the
// Django static tree (Decision 3, docs/meso/designer-framework-plan.md).
// WhiteNoise's manifest storage content-hashes everything again at
// `collectstatic`, so Vite doesn't need to — and a hashed filename here would
// have nowhere to update the `{% static %}` reference in designer.html.
export default defineConfig({
  root: here,
  plugins: [react()],
  build: {
    outDir,
    emptyOutDir: true,
    // Flatten output: dist/designer.js + dist/designer.css, no assets/ dir.
    assetsDir: ".",
    cssCodeSplit: false,
    modulePreload: false,
    target: "es2022",
    rollupOptions: {
      input: `${here}src/main.tsx`,
      output: {
        format: "es",
        // No code-split hashes: one entry, no dynamic imports, so there is
        // exactly one JS chunk and (if any component imports CSS) one
        // stylesheet — both land under these fixed names every build.
        entryFileNames: "designer.js",
        chunkFileNames: "designer.js",
        assetFileNames: "designer[extname]",
        codeSplitting: false,
      },
    },
  },
});
