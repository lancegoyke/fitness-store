// Specs for AthleteMeta (CONTRACT.md "AthleteMeta") — the slim athlete-context
// header (identity + goal + contraindications) at the top of the left sidebar.
// designer-simplify: the macrocycle phase list + "Open plan →" shortcut that
// the old LeftRail carried are folded out (top-bar cycle chip + Periodization
// view own that now), so this component no longer takes phases/onOpenBlockView.
import { render, screen } from "@testing-library/react";
import { AthleteMeta } from "./AthleteMeta";
import type { AthleteIdentity } from "../lib/api";

const athlete: AthleteIdentity = {
  name: "Maya Okonkwo",
  initials: "MO",
  goal: "Strength",
  contraindications: [{ text: "Knee-sensitive" }],
};

describe("AthleteMeta", () => {
  it("renders the athlete's identity, goal (once), and contraindications", () => {
    render(<AthleteMeta athlete={athlete} />);
    expect(screen.getByText("Maya Okonkwo")).toBeInTheDocument();
    // Goal renders a single time now (the duplicate "Goal" tag section is gone).
    expect(screen.getByText("Strength")).toBeInTheDocument();
    expect(screen.getByText("Knee-sensitive")).toBeInTheDocument();
  });

  it("shows 'None noted.' when the athlete has no contraindications", () => {
    render(<AthleteMeta athlete={{ ...athlete, contraindications: [] }} />);
    expect(screen.getByText("None noted.")).toBeInTheDocument();
  });

  it("renders nothing when there is no athlete", () => {
    const { container } = render(<AthleteMeta athlete={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("no longer renders the macrocycle 'Open plan →' shortcut (folded out)", () => {
    render(<AthleteMeta athlete={athlete} />);
    expect(screen.queryByTestId("open-block-view-button")).not.toBeInTheDocument();
  });
});
