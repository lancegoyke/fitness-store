import { useEffect, useMemo, useState } from "react";
import type { MesoGrid } from "../lib/api";
import { gridToProgram } from "../lib/grid";

export interface AthletePreviewProps {
  grid: MesoGrid;
  coachmarkVisible?(key: string): boolean;
  dismissCoachmark?(key: string): void;
}

function textLabel(text: string): string {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .join(" · ") || "—";
}

export function AthletePreview({
  grid,
  coachmarkVisible,
  dismissCoachmark,
}: AthletePreviewProps) {
  const [selectedWeekId, setSelectedWeekId] = useState<number | null>(grid.weeks[0]?.id ?? null);
  const [selectedDayNumber, setSelectedDayNumber] = useState<number | null>(null);
  const selectedWeek = grid.weeks.find((week) => week.id === selectedWeekId) ?? grid.weeks[0];
  const program = useMemo(
    () => gridToProgram(grid, selectedWeek?.id),
    [grid, selectedWeek?.id],
  );
  const selectedDay = program.find((day) => day.n === selectedDayNumber) ?? program[0];

  useEffect(() => {
    const nextWeekId = selectedWeek?.id ?? null;
    if (selectedWeekId !== nextWeekId) setSelectedWeekId(nextWeekId);
  }, [selectedWeek?.id, selectedWeekId]);

  useEffect(() => {
    const nextDayNumber = selectedDay?.n ?? null;
    if (selectedDayNumber !== nextDayNumber) setSelectedDayNumber(nextDayNumber);
  }, [selectedDay?.n, selectedDayNumber]);

  const exercises = selectedDay?.exercises.filter((exercise) => !exercise.skipped) ?? [];
  const blockWeekLabel = selectedWeek
    ? `${grid.mesocycle.name} · ${selectedWeek.label}`
    : grid.mesocycle.name;

  return (
    <div className="meso-athlete-preview">
      {coachmarkVisible?.("phone") && (
        <div className="meso-flex meso-coachmark meso-coachmark--phone">
          <div className="meso-coachmark-body">
            <div className="meso-coachmark-title">Preview as your athlete</div>
            <div className="meso-coachmark-text">
              {selectedWeek?.label ?? "This week"} follows your athlete&apos;s page layout — each day,
              exercise, target, note, and “what you did” line updates as you edit. Set rows and
              logging show up on their phone. Deliver sends them a heads-up.
            </div>
          </div>
          <button
            type="button"
            data-hover="rail"
            className="meso-coachmark-dismiss"
            aria-label="Dismiss tip"
            onClick={() => dismissCoachmark?.("phone")}
          >
            ×
          </button>
        </div>
      )}

      <div className="meso-athlete-preview-controls">
        <div className="meso-seg" role="tablist" aria-label="Preview week">
          {grid.weeks.map((week) => (
            <button
              key={week.id}
              type="button"
              role="tab"
              data-testid={`athlete-preview-week-${week.id}`}
              aria-selected={week.id === selectedWeek?.id}
              className={`meso-seg-btn${week.id === selectedWeek?.id ? " is-on" : ""}`}
              onClick={() => setSelectedWeekId(week.id)}
            >
              {week.label}
            </button>
          ))}
        </div>
        <div className="meso-seg" role="tablist" aria-label="Preview day">
          {program.map((day) => (
            <button
              key={day.id}
              type="button"
              role="tab"
              data-testid={`athlete-preview-day-${day.n}`}
              aria-selected={day.n === selectedDay?.n}
              className={`meso-seg-btn${day.n === selectedDay?.n ? " is-on" : ""}`}
              onClick={() => setSelectedDayNumber(day.n)}
            >
              Day {day.n}
            </button>
          ))}
        </div>
      </div>

      <div className="meso-phone">
        <div className="meso-phone-screen">
          <div className="meso-phone-statusbar">
            <span className="meso-mono">6:14</span>
            <div className="meso-phone-notch" />
            <span className="meso-phone-signal">●●● ◉</span>
          </div>
          <div className="meso-phone-header">
            <div className="meso-phone-blocklabel">{blockWeekLabel}</div>
            {selectedDay && (
              <>
                <div className="meso-phone-daylabel">
                  Day {selectedDay.n}
                  {selectedDay.bias?.trim() ? ` · ${selectedDay.bias}` : ""}
                </div>
                <h2 className="meso-phone-title">{selectedDay.name}</h2>
                <div className="meso-phone-sub">{grid.plan?.title ?? ""}</div>
              </>
            )}
          </div>
          <div className="meso-phone-body">
            {exercises.map((exercise) => {
              const note = exercise.note?.trim();
              const lines = (exercise.lines ?? []).filter((line) => line.text.trim());
              return (
                <div
                  key={exercise.id}
                  className="meso-phone-exercise"
                  data-testid="athlete-preview-exercise-card"
                >
                  <div className="meso-phone-exercise-head">
                    <div className="meso-phone-exercise-name">{exercise.name}</div>
                    <div className="meso-mono meso-phone-exercise-target">
                      target {textLabel(exercise.text)}
                      {note ? ` · ${note}` : ""}
                    </div>
                  </div>
                  <div className="meso-phone-exercise-log">
                    <div className="meso-phone-exercise-log-label">what you did</div>
                    {lines.map((line, lineIndex) => (
                      <input
                        key={line.line}
                        className="meso-phone-exercise-line"
                        data-testid={`athlete-line-${exercise.id}-${lineIndex}`}
                        value={line.text}
                        readOnly
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
