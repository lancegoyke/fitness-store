// LeftRail (CONTRACT.md "LeftRail") — ported 1:1 from designer.html's left
// rail (lines ~103-201): athlete identity block + macrocycle phase list.
import type { AthleteIdentity, Phase } from "../lib/api";

export interface LeftRailProps {
  athlete: AthleteIdentity | null;
  phases: Phase[];
  onOpenBlockView(): void;
}

export function LeftRail({ athlete, phases, onOpenBlockView }: LeftRailProps) {
  return (
    <div className="meso-left-rail">
      {athlete && (
        <div className="meso-rail-block">
          <div className="meso-rail-identity">
            <div className="meso-rail-avatar">{athlete.initials}</div>
            <div className="meso-identity-info">
              <div className="meso-rail-name">{athlete.name}</div>
              {athlete.goal && <div className="meso-identity-goal">{athlete.goal}</div>}
            </div>
          </div>
          {athlete.goal && (
            <div className="meso-rail-section">
              <p className="meso-rail-label">Goal</p>
              <div className="meso-flex meso-wrap">
                <span className="meso-goal-tag">{athlete.goal}</span>
              </div>
            </div>
          )}
          <div className="meso-rail-section">
            <p className="meso-rail-label">Contraindications</p>
            <div className="meso-rail-list">
              {athlete.contraindications.map((c) => (
                <div key={c.text} className="meso-warn-row">
                  <div className="meso-warn-dot" />
                  <div className="meso-warn-text">{c.text}</div>
                </div>
              ))}
              {!athlete.contraindications.length && <div className="meso-rail-empty">None noted.</div>}
            </div>
          </div>
        </div>
      )}

      <div className="meso-rail-divider" />

      <div>
        <div className="meso-rail-section-head">
          <p className="meso-rail-label">Macrocycle</p>
          <button type="button" data-testid="open-block-view-button" className="meso-open-block-btn" onClick={onOpenBlockView}>
            Open plan →
          </button>
        </div>
        <div className="meso-macro-list">
          {phases.map((p) => (
            <div key={p.name} className={`meso-macro-item meso-macro-item--${p.state}`}>
              <div className="meso-macro-dot" />
              <div className="meso-macro-name">{p.name}</div>
              <div className="meso-macro-weeks">
                {p.weeks + (p.state === "current" ? " · now" : p.state === "done" ? " · done" : p.state === "next" ? " · next" : "")}
              </div>
            </div>
          ))}
          {!phases.length && <div className="meso-rail-empty">No blocks yet.</div>}
        </div>
      </div>
    </div>
  );
}
