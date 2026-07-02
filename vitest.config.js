import { defineConfig } from "vitest/config";

// Front-end unit tests for the meso app's hand-written JS (the only place in
// the repo with non-trivial client-side logic: the athlete's offline log queue
// and the designer's agent poll loop). Tests live under frontend/ — kept out of
// app/store_project/static/ so they are never collected/served by Django, and
// so test-only changes can be excluded from the deploy-triggering Django CI.
export default defineConfig({
  test: {
    globals: true,
    environment: "jsdom",
    include: ["frontend/**/*.test.js"],
    coverage: {
      provider: "v8",
      include: ["app/store_project/static/js/meso.js", "app/store_project/static/js/meso_athlete.js", "app/store_project/static/js/meso_onboarding.js", "app/store_project/static/js/meso_deliver.js"],
      reporter: ["text", "html"],
    },
  },
});
