// LeftRail (CONTRACT.md "LeftRail") — ported 1:1 from designer.html's left
// rail (lines ~103-201): athlete/group identity block + macrocycle phase list.
import type { AthleteIdentity, GroupIdentity, Phase } from "../lib/api";

export interface LeftRailProps {
  isIndividual: boolean;
  isGroup: boolean;
  athlete: AthleteIdentity | null;
  group: GroupIdentity | null;
  phases: Phase[];
  onOpenBlockView(): void;
}

export function LeftRail({ isIndividual, isGroup, athlete, group, phases, onOpenBlockView }: LeftRailProps) {
  return (
    <div className="meso-left-rail">
      {isIndividual && athlete && (
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

      {isGroup && group && (
        <div className="meso-rail-block">
          <div className="meso-rail-identity">
            <div className="meso-rail-avatar meso-rail-avatar--group">
              <span className="meso-rail-group-glyph" />
            </div>
            <div className="meso-identity-info">
              <div className="meso-rail-name">{group.name}</div>
              <div className="meso-identity-goal">
                {group.member_count + (group.member_count === 1 ? " participant" : " participants") +
                  (group.focus ? " · " + group.focus : "")}
              </div>
            </div>
          </div>
          <div className="meso-rail-section">
            <p className="meso-rail-label">Participants</p>
            <div className="meso-rail-list meso-rail-list--tight">
              {group.members.map((m, i) => (
                <div key={m.id} className="meso-participant-row">
                  <div className={`meso-participant-avatar${i === 0 ? " meso-participant-avatar--lead" : ""}`}>{m.initials}</div>
                  <span>{m.name}</span>
                </div>
              ))}
              {!group.members.length && <div className="meso-rail-empty">No active members yet.</div>}
            </div>
          </div>
          {!!group.flags.length && (
            <div className="meso-rail-section">
              <p className="meso-rail-label">Flags across group</p>
              <div className="meso-flex meso-wrap">
                {group.flags.map((f) => (
                  <span key={f} className="meso-flag-badge">
                    <span className="meso-flag-dot" />
                    <span>{f}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
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
