// AthleteMeta (CONTRACT.md "AthleteMeta") — the slim athlete-context header at
// the top of the left sidebar: identity (avatar + name + goal) and the
// contraindications list. It shares the sidebar column with the agent
// ChatPanel stacked below it. (designer-simplify: the old full-height LeftRail
// is gone — its macrocycle phase list folded out to the top-bar cycle chip +
// the Periodization view, and its duplicate goal tag / "Open plan →" shortcut
// were dropped as redundant with the segmented control.)
import type { AthleteIdentity } from "../lib/api";

export interface AthleteMetaProps {
  athlete: AthleteIdentity | null;
}

export function AthleteMeta({ athlete }: AthleteMetaProps) {
  if (!athlete) return null;
  return (
    <div className="meso-athlete-meta" data-testid="athlete-meta">
      <div className="meso-meta-identity">
        <div className="meso-meta-avatar">{athlete.initials}</div>
        <div className="meso-meta-info">
          <div className="meso-meta-name">{athlete.name}</div>
          {athlete.goal && <div className="meso-meta-goal">{athlete.goal}</div>}
        </div>
      </div>
      <div className="meso-meta-section">
        <p className="meso-meta-label">Contraindications</p>
        <div className="meso-meta-list">
          {athlete.contraindications.map((c) => (
            <div key={c.text} className="meso-warn-row">
              <div className="meso-warn-dot" />
              <div className="meso-warn-text">{c.text}</div>
            </div>
          ))}
          {!athlete.contraindications.length && <div className="meso-meta-empty">None noted.</div>}
        </div>
      </div>
    </div>
  );
}
