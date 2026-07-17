// AthletePreview (CONTRACT.md "AthletePreview") — ported 1:1 from
// designer.html's phone mock (lines ~536-599). `athleteDay`/`aTotal`/`aDone`
// were getters on createMeso() (meso.js); ported here as a useMemo since
// they're view-shaping with no existing lib coverage (CONTRACT.md).
import { useMemo } from "react";
import type { Day } from "../lib/api";

export interface AthletePreviewProps {
  program: Day[];
  unit: string;
  checks: Record<string, boolean>;
  onToggleCheck(key: string): void;
  // Phone-preview coachmark ("phone" key) — same plumbing as WeekGrid's
  // "grid" coachmark. Optional so isolated renders stay coachmark-free.
  coachmarkVisible?(key: string): boolean;
  dismissCoachmark?(key: string): void;
}

interface AthleteSetRow {
  k: string;
  n: number;
  target: string;
  done: boolean;
}

interface AthleteExerciseRow {
  id: number | string;
  name: string;
  target: string;
  /** The cell's freeform sub-lines (RPE rows, cues, substitutions as text). */
  lines: string[];
  rows: AthleteSetRow[];
}

export function AthletePreview({
  program,
  unit,
  checks,
  onToggleCheck,
  coachmarkVisible,
  dismissCoachmark,
}: AthletePreviewProps) {
  const athleteDay = useMemo<AthleteExerciseRow[]>(() => {
    const day = program[0];
    if (!day) return [];
    // A skipped exercise (P2 one-week exception) isn't trained this week — the
    // real athlete surface filters these server-side, so the coach's preview
    // must not show a lift the athlete won't actually see.
    // Phase 2a text-first: the prescription is one freeform string (plus
    // optional sub-lines) — there's no sets count left to fan set rows out
    // from, so the mock shows one loggable row per lift with the verbatim
    // text as its target ("4 x 6, RPE 9, 225" reads as-written, same as the
    // source spreadsheet).
    return day.exercises
      .filter((x) => !x.skipped)
      .slice(0, 3)
      .map((x, xi) => {
        const target = x.text || "—";
        const k = "a0-" + xi + "-0";
        const lines = (x.lines ?? []).map((l) => l.text).filter((t) => t.trim() !== "");
        return {
          id: x.id,
          name: x.name,
          target,
          lines,
          rows: [{ k, n: 1, target, done: !!checks[k] }],
        };
      });
  }, [program, checks]);

  const aTotal = athleteDay.reduce((acc, e) => acc + e.rows.length, 0);
  const aDone = athleteDay.reduce((acc, e) => acc + e.rows.filter((r) => r.done).length, 0);
  const day0 = program[0];

  return (
    <div className="meso-athlete-preview">
      {coachmarkVisible?.("phone") && (
        <div className="meso-flex meso-coachmark meso-coachmark--phone">
          <div className="meso-coachmark-body">
            <div className="meso-coachmark-title">Preview as your athlete</div>
            <div className="meso-coachmark-text">
              This is exactly what your athlete sees on their phone — your edits are live the moment you type them. Deliver sends them a heads-up.
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
      <div className="meso-phone">
        <div className="meso-phone-screen">
          <div className="meso-phone-statusbar">
            <span className="meso-mono">6:14</span>
            <div className="meso-phone-notch" />
            <span className="meso-phone-signal">●●● ◉</span>
          </div>
          <div className="meso-phone-header">
            <div className="meso-phone-header-row">
              <span className="meso-phone-back">← Coach</span>
              <div className="meso-phone-daylabel">Wed · Day 1</div>
            </div>
            <h2 className="meso-phone-title">{day0 ? day0.name : "Lower · Quad bias"}</h2>
            <div className="meso-phone-sub">Box squat focus · knee-safe</div>
            <div className="meso-flex meso-phone-progress">
              <div className="meso-mono meso-phone-count">
                {aDone}/{aTotal}
              </div>
              <div className="meso-flex meso-phone-dots">
                {athleteDay.map((e) => e.rows.map((r) => <div key={r.k} className={`meso-phone-dot${r.done ? " is-done" : ""}`} />))}
              </div>
              <div className="meso-phone-setslabel">sets</div>
            </div>
          </div>
          <div className="meso-phone-body">
            {athleteDay.map((ae) => (
              <div key={ae.id} className="meso-phone-exercise">
                <div className="meso-phone-exercise-head">
                  <div className="meso-phone-exercise-name">{ae.name}</div>
                  <div className="meso-mono meso-phone-exercise-target">{"target " + ae.target}</div>
                </div>
                {ae.lines.map((line, li) => (
                  <div key={li} className="meso-phone-exercise-line" data-testid={`athlete-line-${ae.id}-${li}`}>
                    {line}
                  </div>
                ))}
                {ae.rows.map((r) => (
                  <div key={r.k} className="meso-phone-set-row">
                    <div className="meso-mono meso-phone-set-n">{r.n}</div>
                    <input className="meso-phone-input" placeholder="reps" readOnly />
                    <span className="meso-x-sep">×</span>
                    <input className="meso-phone-input" placeholder={unit} readOnly />
                    <div className="meso-phone-set-target">{r.target}</div>
                    <button
                      type="button"
                      data-testid={`athlete-check-${r.k}`}
                      className="meso-phone-check"
                      onClick={() => onToggleCheck(r.k)}
                    >
                      {r.done ? <div className="meso-check-on">✓</div> : <div className="meso-check-off" />}
                    </button>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
