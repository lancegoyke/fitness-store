// Smoke test for the Phase 2 build toolchain (docs/meso/designer-framework-plan.md,
// Phase 2 / Decision 3): proves jsdom + React Testing Library + TSX compile and
// render through vitest before any real component-port work starts. PR A only
// needs the pipeline to work end-to-end; PR B replaces DesignerRoot with the
// ported island.
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { DesignerRoot } from "./DesignerRoot";

describe("DesignerRoot", () => {
  it("mounts and renders the scaffold placeholder", () => {
    render(<DesignerRoot />);

    expect(screen.getByTestId("designer-root-placeholder")).toBeInTheDocument();
  });
});
