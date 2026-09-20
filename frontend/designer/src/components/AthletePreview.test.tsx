import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MesoGrid } from "../lib/api";
import { AthletePreview } from "./AthletePreview";

function grid(): MesoGrid {
  return {
    plan: { id: 7, title: "Build to Nationals", unit: "kg" },
    mesocycle: { id: 4, plan_id: 7, name: "Accumulation", week_count: 2 },
    weeks: [
      { id: 10, index: 0, label: "Wk 1", phase: "Build", deload: false, delivered_at: null },
      { id: 20, index: 1, label: "Wk 2", phase: "Build", deload: false, delivered_at: null },
    ],
    days: [
      {
        session_slot_id: 1,
        session_id: 1001,
        session_ids: { "10": 1001, "20": 2001 },
        day_number: 1,
        name: "Lower Strength",
        bias: "Squat focus",
        order: 0,
        rows: [
          {
            exercise_slot_id: 101,
            name: "Back Squat",
            exercise_id: 1,
            order: 0,
            tags: [],
            tempo: "",
            rest: "",
            note: "Brace hard",
            cells: {
              "10": {
                prescription_id: 1010,
                text: "4 x 6\nRPE 7",
                skipped: false,
                lines: [
                  { id: 1, line: 1, text: "Pause the first rep", athlete_authored: false },
                  { id: 2, line: 2, text: "100 x 6", athlete_authored: true },
                  { id: 3, line: 3, text: "   ", athlete_authored: false },
                ],
              },
              "20": { prescription_id: 1020, text: "5 x 4, RPE 8", skipped: false, lines: [] },
            },
          },
          {
            exercise_slot_id: 102,
            name: "Romanian Deadlift",
            exercise_id: 2,
            order: 1,
            tags: [],
            tempo: "",
            rest: "",
            note: "",
            cells: {
              "10": { prescription_id: 2010, text: "3 x 8", skipped: false, lines: [] },
              "20": { prescription_id: 2020, text: "3 x 7", skipped: false, lines: [] },
            },
          },
          {
            exercise_slot_id: 103,
            name: "Leg Press",
            exercise_id: 3,
            order: 2,
            tags: [],
            tempo: "",
            rest: "",
            note: "",
            cells: {
              "10": { prescription_id: 3010, text: "3 x 12", skipped: false, lines: [] },
              "20": { prescription_id: 3020, text: "3 x 10", skipped: false, lines: [] },
            },
          },
          {
            exercise_slot_id: 104,
            name: "Calf Raise",
            exercise_id: 4,
            order: 3,
            tags: [],
            tempo: "",
            rest: "",
            note: "",
            cells: {
              "10": { prescription_id: 4010, text: "3 x 15", skipped: false, lines: [] },
              "20": { prescription_id: 4020, text: "3 x 15", skipped: false, lines: [] },
            },
          },
          {
            exercise_slot_id: 105,
            name: "Skipped Curl",
            exercise_id: 5,
            order: 4,
            tags: [],
            tempo: "",
            rest: "",
            note: "",
            cells: {
              "10": { prescription_id: 5010, text: "2 x 12", skipped: true, lines: [] },
              "20": { prescription_id: 5020, text: "2 x 12", skipped: true, lines: [] },
            },
          },
        ],
      },
      {
        session_slot_id: 2,
        session_id: 1002,
        session_ids: { "10": 1002, "20": 2002 },
        day_number: 2,
        name: "Upper Strength",
        bias: "",
        order: 1,
        rows: [
          {
            exercise_slot_id: 201,
            name: "Bench Press",
            exercise_id: 6,
            order: 0,
            tags: [],
            tempo: "",
            rest: "",
            note: "",
            cells: {
              "10": { prescription_id: 6010, text: "4 x 6", skipped: false, lines: [] },
              "20": { prescription_id: 6020, text: "5 x 5", skipped: false, lines: [] },
            },
          },
        ],
      },
      {
        session_slot_id: 3,
        session_id: 1003,
        session_ids: { "10": 1003 },
        day_number: 3,
        name: "Accessories",
        bias: "Arms",
        order: 2,
        rows: [
          {
            exercise_slot_id: 301,
            name: "Cable Curl",
            exercise_id: 7,
            order: 0,
            tags: [],
            tempo: "",
            rest: "",
            note: "",
            cells: {
              "10": { prescription_id: 7010, text: "3 x 12", skipped: false, lines: [] },
              "20": { prescription_id: 7020, text: "3 x 10", skipped: false, lines: [] },
            },
          },
        ],
      },
    ],
    history: { can_undo: false, can_redo: false, undo_label: "", redo_label: "" },
  };
}

function baseProps(overrides: Partial<Parameters<typeof AthletePreview>[0]> = {}) {
  return { grid: grid(), ...overrides };
}

describe("AthletePreview", () => {
  it("switches weeks and days and shows every exercise", async () => {
    const user = userEvent.setup();
    render(<AthletePreview {...baseProps()} />);

    expect(screen.getAllByTestId("athlete-preview-exercise-card")).toHaveLength(4);
    expect(screen.getByText("target 4 x 6 · RPE 7 · Brace hard")).toBeInTheDocument();

    await user.click(screen.getByTestId("athlete-preview-week-20"));
    expect(screen.getByText("target 5 x 4, RPE 8 · Brace hard")).toBeInTheDocument();
    expect(screen.queryByText("target 4 x 6 · RPE 7 · Brace hard")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("athlete-preview-day-2"));
    expect(screen.getByText("Bench Press")).toBeInTheDocument();
    expect(screen.queryByText("Back Squat")).not.toBeInTheDocument();
  });

  it("mirrors the real session header without fabricated copy", async () => {
    const user = userEvent.setup();
    const { container } = render(<AthletePreview {...baseProps()} />);

    expect(screen.getByText("Accumulation · Wk 1")).toBeInTheDocument();
    expect(screen.getByText("Day 1 · Squat focus")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Lower Strength" })).toBeInTheDocument();
    expect(screen.getByText("Build to Nationals")).toBeInTheDocument();

    await user.click(screen.getByTestId("athlete-preview-day-2"));
    expect(container.querySelector(".meso-phone-daylabel")).toHaveTextContent("Day 2");
    expect(container.querySelector(".meso-phone-daylabel")).not.toHaveTextContent("Day 2 ·");
    expect(screen.queryByText(/Wed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/knee-safe/)).not.toBeInTheDocument();
  });

  it("folds target text, appends only non-blank notes, and omits unavailable training", async () => {
    const user = userEvent.setup();
    render(<AthletePreview {...baseProps()} />);

    expect(screen.getByText("target 4 x 6 · RPE 7 · Brace hard")).toBeInTheDocument();
    expect(screen.getByText("target 3 x 8")).toBeInTheDocument();
    expect(screen.queryByText("target 3 x 8 ·")).not.toBeInTheDocument();
    expect(screen.queryByText("Skipped Curl")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("athlete-preview-week-20"));
    expect(screen.queryByTestId("athlete-preview-day-3")).not.toBeInTheDocument();
    expect(screen.queryByText("Cable Curl")).not.toBeInTheDocument();
  });

  it("shows coach and athlete-authored sub-lines under what you did", () => {
    render(<AthletePreview {...baseProps()} />);

    expect(screen.getAllByText("what you did")).toHaveLength(4);
    expect(screen.getByTestId("athlete-line-1010-0")).toHaveValue("Pause the first rep");
    expect(screen.getByTestId("athlete-line-1010-1")).toHaveValue("100 x 6");
    expect(screen.queryByDisplayValue("   ")).not.toBeInTheDocument();
  });

  it("falls back to the first live week and day when a selection disappears", async () => {
    const user = userEvent.setup();
    const initial = grid();
    const { rerender } = render(<AthletePreview grid={initial} />);
    await user.click(screen.getByTestId("athlete-preview-week-20"));
    await user.click(screen.getByTestId("athlete-preview-day-2"));

    const next = grid();
    next.weeks = [next.weeks[0]!];
    next.days[0]!.session_ids = { "10": 1001 };
    next.days[1]!.session_ids = {};
    rerender(<AthletePreview grid={next} />);

    expect(screen.getByTestId("athlete-preview-week-10")).toHaveClass("is-on");
    expect(screen.getByTestId("athlete-preview-day-1")).toHaveClass("is-on");
    expect(screen.getByRole("heading", { name: "Lower Strength" })).toBeInTheDocument();
  });
});

describe("AthletePreview phone coachmark", () => {
  function coachmarkProps(visible: boolean) {
    return baseProps({
      coachmarkVisible: vi.fn((key: string) => visible && key === "phone"),
      dismissCoachmark: vi.fn(),
    });
  }

  it("describes the selected week without claiming exact parity", async () => {
    const user = userEvent.setup();
    render(<AthletePreview {...coachmarkProps(true)} />);

    expect(screen.getByText("Preview as your athlete")).toBeInTheDocument();
    expect(screen.getByText(/Wk 1 follows/)).toBeInTheDocument();
    expect(screen.queryByText(/exactly/i)).not.toBeInTheDocument();

    await user.click(screen.getByTestId("athlete-preview-week-20"));
    expect(screen.getByText(/Wk 2 follows/)).toBeInTheDocument();
  });

  it("hides the coachmark when it is not visible", () => {
    render(<AthletePreview {...coachmarkProps(false)} />);
    expect(screen.queryByText("Preview as your athlete")).not.toBeInTheDocument();
  });

  it("reports the phone key when dismissed", async () => {
    const user = userEvent.setup();
    const props = coachmarkProps(true);
    render(<AthletePreview {...props} />);
    await user.click(screen.getByLabelText("Dismiss tip"));
    expect(props.dismissCoachmark).toHaveBeenCalledWith("phone");
  });
});
