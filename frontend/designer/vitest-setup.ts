// Extends vitest's `expect` with jest-dom matchers (toBeInTheDocument, etc.)
// for the designer island's React Testing Library specs. Loaded for every
// frontend/**/*.test.{js,ts,tsx} file via vitest.config.js's `setupFiles` —
// harmless no-op for the plain-JS meso*.test.js suites, which don't use it.
import "@testing-library/jest-dom/vitest";
