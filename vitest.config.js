import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Front-end unit tests for the meso app's hand-written JS (the athlete's
// offline log queue, onboarding, and deliver logic — the designer's own
// agent poll loop, delete verbs, and undo/redo moved to the React island
// under frontend/designer/ in Phase 2 PR B, retiring meso.js and its three
// meso*.test.js suites) AND the Phase 2 designer island under
// frontend/designer/ (React + TSX, built separately by
// frontend/designer/vite.config.ts — see Decision 3 in
// docs/meso/designer-framework-plan.md). Tests live under frontend/ — kept out
// of app/store_project/static/ so they are never collected/served by Django,
// and so test-only changes can be excluded from the deploy-triggering Django CI.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    include: ["frontend/**/*.test.{js,ts,tsx}"],
    setupFiles: ["frontend/designer/vitest-setup.ts"],
    coverage: {
      provider: "v8",
      include: ["app/store_project/static/js/meso_athlete.js", "app/store_project/static/js/meso_onboarding.js", "app/store_project/static/js/meso_deliver.js"],
      reporter: ["text", "html"],
    },
  },
});
