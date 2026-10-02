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

const GENERIC_PLACEHOLDER = "225 x 5, RPE 8 — or a note";
const MAX_LINES = 20;

/** The athlete-format placeholder for a cell's first line, or the generic one.
 * Only a plain "NxM @ load" (integer reps, absolute numeric load) converts:
 * "3x5 @ 225" -> "225 x 5". Ranges, AMRAP, bw, % targets fall back. */
export function athletePlaceholder(text: string): string {
  const first = text.split(/\r?\n/)[0]?.trim() ?? "";
  const m = /^(\d+)\s*[x×]\s*(\d+)\s*@\s*(\d+(?:\.\d+)?)\s*(?:kg|lbs?)?$/i.exec(first);
  return m ? `${m[3]} x ${m[2]}` : GENERIC_PLACEHOLDER;
}

/** How many empty lines the athlete's page pads (mirrors the server's
 * `pad_lines`: the prescribed set count, 3 when it has none, clamped 1-12). */
function prescribedSets(text: string): number {
  const first = text.split(/\r?\n/)[0]?.trim() ?? "";
  const m = /^(\d+)\s*[x×]/i.exec(first);
  const n = m ? Number(m[1]) : 0;
  return Math.max(1, Math.min(n || 3, 12));
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
              exercise, target, note, and “what you did” line updates as you edit.
              Logging happens on their phone. Deliver sends them a heads-up.
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
              const sub = (exercise.lines ?? []).filter((line) => line.text.trim());
              const athleteLines = sub.filter((line) => line.athlete_authored);
              const cues = sub.filter((line) => !line.athlete_authored);
              // Empty lines never reuse a number a cue or an athlete line holds.
              const taken = new Set(sub.map((line) => line.line));
              const emptyCount = Math.max(
                1,
                Math.min(prescribedSets(exercise.text) - athleteLines.length, MAX_LINES - sub.length),
              );
              const emptyLines: number[] = [];
              for (let n = 1; emptyLines.length < emptyCount && n <= MAX_LINES; n++) {
                if (!taken.has(n)) emptyLines.push(n);
              }
              const placeholder = athletePlaceholder(exercise.text);
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
                    {athleteLines.map((line, lineIndex) => (
                      <input
                        key={line.line}
                        className="meso-phone-exercise-line"
                        data-testid={`athlete-line-${exercise.id}-${lineIndex}`}
                        value={line.text}
                        readOnly
                      />
                    ))}
                    {emptyLines.map((n) => (
                      <input
                        key={`empty-${n}`}
                        className="meso-phone-exercise-line"
                        data-testid={`athlete-empty-line-${exercise.id}-${n}`}
                        data-line={n}
                        placeholder={placeholder}
                        value=""
                        readOnly
                      />
                    ))}
                    <button
                      type="button"
                      className="meso-btn meso-btn--ghost"
                      style={{ padding: "4px 8px", fontSize: 12 }}
                      disabled
                    >
                      + add a line
                    </button>
                  </div>
                  {cues.length > 0 && (
                    <div
                      className="meso-phone-exercise-log"
                      data-testid={`athlete-cues-${exercise.id}`}
                      style={{ borderTop: "1px solid var(--line-2)" }}
                    >
                      <div className="meso-phone-exercise-log-label">from your coach</div>
                      {cues.map((cue) => (
                        <div
                          key={cue.line}
                          className="meso-mono"
                          data-testid={`athlete-cue-${exercise.id}-${cue.line}`}
                          style={{ fontSize: 12, color: "var(--dim)", marginBottom: 4 }}
                        >
                          {cue.text}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
            <div className="meso-phone-exercise-log" data-testid="athlete-preview-notes">
              <label className="meso-phone-exercise-log-label" htmlFor="athlete-preview-notes-box">
                Notes for your coach
              </label>
              <textarea
                id="athlete-preview-notes-box"
                className="meso-phone-exercise-line"
                rows={3}
                disabled
                placeholder="Logging happens on their phone"
              />
            </div>
            <button type="button" className="meso-btn meso-btn--primary" disabled>
              Finish session
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
