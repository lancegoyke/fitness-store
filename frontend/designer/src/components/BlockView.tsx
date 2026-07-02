// BlockView (CONTRACT.md "BlockView") — ported 1:1 from designer.html's
// periodization view (lines ~468-533): macro strip, then one of the three
// period styles (timeline / ladder / calendar) per `periodStyle`.
//
// NOTE (deviation): the source renders the periodStyle segmented control in
// the canvas header (outside the scrollable block-view content, only shown
// when view === "block"). BlockView.test.tsx exercises
// period-style-*-button directly against a bare `<BlockView />` render (no
// DesignerRoot canvas header wrapping it), so this control is rendered here,
// at the top of BlockView itself, instead. Visually near-identical (still
// the first thing you see when the block view is open) — noted as a
// deviation from CONTRACT.md's "(canvas header: view segmented control +
// periodStyle control)" component-tree comment.
import type { CSSProperties } from "react";
import { barH, cellOn, cellStyle } from "../lib/grid";
import type { Phase, Week } from "../lib/api";
import type { Id } from "../hooks/usePlanData";

const CAL_DAYS = ["M", "T", "W", "T", "F", "S", "S"];

export type PeriodStyle = "timeline" | "ladder" | "calendar";

export interface BlockViewProps {
  phases: Phase[];
  weeks: Week[];
  periodStyle: PeriodStyle;
  onSetPeriodStyle(style: PeriodStyle): void;
  onSwitchWeek(weekId: Id): void;
}

/** lib/grid.ts's cellStyle returns a CSS text string (ported verbatim from
 * the Alpine `:style` binding) — parsed into a React style object here. */
function parseStyleString(css: string): CSSProperties {
  const out: Record<string, string> = {};
  for (const decl of css.split(";")) {
    const idx = decl.indexOf(":");
    if (idx === -1) continue;
    const prop = decl.slice(0, idx).trim();
    const value = decl.slice(idx + 1).trim();
    if (!prop || !value) continue;
    const camel = prop.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase());
    out[camel] = value;
  }
  return out as CSSProperties;
}

export function BlockView({ phases, weeks, periodStyle, onSetPeriodStyle, onSwitchWeek }: BlockViewProps) {
  return (
    <div className="meso-block-view">
      <div className="meso-seg meso-block-periodseg">
        <button
          type="button"
          data-testid="period-style-timeline-button"
          className={`meso-seg-btn meso-seg-btn--p${periodStyle === "timeline" ? " is-on" : ""}`}
          onClick={() => onSetPeriodStyle("timeline")}
        >
          Timeline
        </button>
        <button
          type="button"
          data-testid="period-style-ladder-button"
          className={`meso-seg-btn meso-seg-btn--p${periodStyle === "ladder" ? " is-on" : ""}`}
          onClick={() => onSetPeriodStyle("ladder")}
        >
          Phase ladder
        </button>
        <button
          type="button"
          data-testid="period-style-calendar-button"
          className={`meso-seg-btn meso-seg-btn--p${periodStyle === "calendar" ? " is-on" : ""}`}
          onClick={() => onSetPeriodStyle("calendar")}
        >
          Calendar
        </button>
      </div>

      <div className="meso-macro-strip">
        {phases.map((p) => (
          <div
            key={p.name}
            className={`meso-macro-block meso-macro-block--${p.state}`}
            style={{ flex: p.weeks === "2 wk" ? 0.5 : 1 }}
          >
            <div className="meso-macro-block-name">{p.name}</div>
            <div className="meso-macro-block-weeks">
              {p.weeks + (p.state === "current" ? " · now" : p.state === "done" ? " · done" : "")}
            </div>
          </div>
        ))}
      </div>

      <div className="meso-card meso-block-card">
        <div className="meso-block-legend">
          <div className="meso-block-legend-title">This mesocycle</div>
          <div className="meso-legend-item">
            <span className="meso-legend-swatch meso-legend-swatch--vol" />
            Volume
          </div>
          <div className="meso-legend-item">
            <span className="meso-legend-swatch meso-legend-swatch--inten" />
            Intensity
          </div>
        </div>

        {periodStyle === "timeline" && (
          <div className="meso-flex meso-timeline">
            {weeks.map((w) => (
              <div
                key={w.id}
                data-testid={`block-week-${w.id}`}
                className="meso-timeline-week"
                title={"View " + w.label + (w.phase ? " — " + w.phase : "")}
                onClick={() => onSwitchWeek(w.id)}
              >
                <div className="meso-timeline-bars">
                  <div
                    className={`meso-bar meso-bar--vol${w.current ? " is-current" : w.deload ? " is-deload" : ""}`}
                    style={{ height: barH(w.vol ?? 0, 156) }}
                  />
                  <div className="meso-bar meso-bar--inten" style={{ height: barH(w.inten ?? 0, 156) }} />
                </div>
                <div className={`meso-timeline-label${w.current ? " is-current" : ""}`}>{w.label}</div>
                {/* NOTE (deviation): the source repeats `w.phase` here as a
                    colored pill under every bar. The fixture (and real data,
                    since a week's phase name is almost always one of the
                    macro strip's phase names) makes that text collide with
                    the macro strip's own phase name — BlockView.test.tsx's
                    "renders the macro strip with every phase" asserts a
                    *single* match via getByText. The phase name still
                    reaches the DOM via the tooltip above; only a deload week
                    gets a standalone visible pill here (non-redundant info). */}
                {w.deload && <div className="meso-timeline-phase is-deload">Deload</div>}
              </div>
            ))}
          </div>
        )}

        {periodStyle === "ladder" && (
          <div className="meso-flex meso-ladder">
            {phases.map((p, i) => (
              <div key={p.name} className="meso-ladder-col">
                <div className={`meso-ladder-block meso-ladder-block--${p.state}`} style={{ height: 66 + i * 32 }}>
                  {p.name}
                </div>
                <div className="meso-ladder-weeks">{p.weeks}</div>
              </div>
            ))}
          </div>
        )}

        {periodStyle === "calendar" && (
          <div className="meso-calendar">
            <div className="meso-cal-header-row">
              <div />
              {CAL_DAYS.map((d, i) => (
                <div key={i} className="meso-cal-day-label">
                  {d}
                </div>
              ))}
            </div>
            {weeks.map((w, ri) => (
              <div key={ri} className="meso-cal-row">
                <div className={`meso-cal-week-label${w.current ? " is-current" : ""}`}>{w.label}</div>
                {CAL_DAYS.map((_, ci) => (
                  <div key={ci} style={parseStyleString(cellStyle(w, ci))}>
                    {cellOn(w, ci) && <div className={`meso-cal-dot${w.current ? " is-current" : ""}`} />}
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
