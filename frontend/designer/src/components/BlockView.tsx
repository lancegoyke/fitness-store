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
import { useState } from "react";
import type { CSSProperties } from "react";
import { barH, cellOn, cellStyle } from "../lib/grid";
import type { GridDay, GridWeek, Phase } from "../lib/api";
import type { Id } from "../hooks/useGrid";

export type PeriodStyle = "timeline" | "ladder" | "calendar";

export interface BlockViewProps {
  phases: Phase[];
  weeks: GridWeek[];
  days: GridDay[];
  periodStyle: PeriodStyle;
  onSetPeriodStyle(style: PeriodStyle): void;
  onRenameMesocycle(mesocycleId: Id, name: string): void;
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

function BlockNameEditor({
  phase,
  onRename,
}: {
  phase: Phase;
  onRename(mesocycleId: Id, name: string): void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(phase.name);

  const beginEdit = () => {
    setDraft(phase.name);
    setEditing(true);
  };
  const cancelEdit = () => {
    setDraft(phase.name);
    setEditing(false);
  };
  const commitEdit = () => {
    const name = draft.trim();
    setEditing(false);
    if (!name) {
      setDraft(phase.name);
      return;
    }
    setDraft(name);
    if (name !== phase.name) onRename(phase.id, name);
  };

  return editing ? (
    <input
      autoFocus
      aria-label="Block name"
      className="meso-block-name-input"
      maxLength={255}
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commitEdit}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          commitEdit();
        } else if (event.key === "Escape") {
          event.preventDefault();
          cancelEdit();
        }
      }}
    />
  ) : (
    <button
      type="button"
      className="meso-block-name-button"
      aria-label={`Rename block: ${phase.name}`}
      title="Rename block"
      onClick={beginEdit}
    >
      {phase.name}
    </button>
  );
}

export function BlockView({
  phases,
  weeks,
  days,
  periodStyle,
  onSetPeriodStyle,
  onRenameMesocycle,
  onSwitchWeek,
}: BlockViewProps) {
  const currentPhase = phases.find((phase) => phase.state === "current") ?? phases[0];

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
            key={p.id}
            className={`meso-macro-block meso-macro-block--${p.state}`}
            style={{ flex: p.weeks === "2 wk" ? 0.5 : 1 }}
          >
            <div className="meso-macro-block-name">
              <BlockNameEditor phase={p} onRename={onRenameMesocycle} />
            </div>
            <div className="meso-macro-block-weeks">
              {p.weeks + (p.state === "current" ? " · now" : p.state === "done" ? " · done" : "")}
            </div>
          </div>
        ))}
      </div>

      <div className="meso-card meso-block-card">
        <div className="meso-block-legend">
          <div className="meso-block-legend-title">
            This mesocycle{currentPhase ? ` · ${currentPhase.name}` : ""}
          </div>
          <div className="meso-legend-item">
            <span className="meso-legend-swatch meso-legend-swatch--vol" />
            Volume
          </div>
          <div className="meso-legend-item">
            <span className="meso-legend-swatch meso-legend-swatch--inten" />
            Intensity
          </div>
        </div>
        {periodStyle === "timeline" && weeks.some((w) => w.vol === null || w.inten === null) && (
          <div className="meso-block-chart-caption" data-testid="block-chart-caption">
            Bars are read from the sets, reps and loads you've written. Weeks with none show no bar.
          </div>
        )}

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
                  {w.vol !== null && (
                    <div
                      className={`meso-bar meso-bar--vol${w.deload ? " is-deload" : ""}`}
                      style={{ height: barH(w.vol ?? 0, 156) }}
                    />
                  )}
                  {w.inten !== null && (
                    <div className="meso-bar meso-bar--inten" style={{ height: barH(w.inten ?? 0, 156) }} />
                  )}
                </div>
                {w.vol === null && w.inten === null && (
                  <div className="meso-timeline-nodata" data-testid={`block-week-nodata-${w.id}`}>
                    Nothing to chart — no sets × reps or loads parsed
                  </div>
                )}
                <div className="meso-timeline-label">{w.label}</div>
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
              <div key={p.id} className="meso-ladder-col">
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
            <div className="meso-cal-header-row" style={{ gridTemplateColumns: `50px repeat(${days.length}, 1fr)` }}>
              <div />
              {days.map((day) => (
                <div key={day.session_slot_id} className="meso-cal-day-label">
                  {day.name?.trim() || `Day ${day.day_number}`}
                </div>
              ))}
            </div>
            {weeks.map((w) => (
              <div
                key={w.id}
                className="meso-cal-row"
                style={{ gridTemplateColumns: `50px repeat(${days.length}, 1fr)` }}
              >
                <div className="meso-cal-week-label">{w.label}</div>
                {days.map((day) => (
                  <div key={day.session_slot_id} style={parseStyleString(cellStyle(day, w))}>
                    {cellOn(day, w) && <div className="meso-cal-dot" />}
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
