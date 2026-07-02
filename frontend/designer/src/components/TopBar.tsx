// TopBar (CONTRACT.md "TopBar") — ported 1:1 from designer.html's top bar
// (lines ~42-98): brand/back-link, individual/group identity chip, mode
// segmented control, cycle label, preview/review/deliver actions.
import type { AthleteIdentity, GroupIdentity } from "../lib/api";

export type DesignerMode = "individual" | "group";

export interface TopBarProps {
  mode: DesignerMode;
  onSetMode(mode: DesignerMode): void;
  isIndividual: boolean;
  isGroup: boolean;
  athlete: AthleteIdentity | null;
  group: GroupIdentity | null;
  cycleLabel: string;
  onPreviewAsAthlete(): void;
  deliverHref: string;
}

export function TopBar(props: TopBarProps) {
  const { mode, onSetMode, isIndividual, isGroup, athlete, group, cycleLabel, onPreviewAsAthlete, deliverHref } = props;

  return (
    <div className="meso-topbar">
      <a href="/meso/" title="Back to roster" className="meso-topbar-brand">
        <span className="meso-topbar-mark">
          <span className="meso-topbar-mark-glyph" />
        </span>
        <span className="meso-topbar-title">Meso</span>
      </a>
      <div className="meso-topbar-divider" />

      <div className="meso-identity-slot">
        {isIndividual && athlete && (
          <div className="meso-identity">
            <div className="meso-identity-avatar">{athlete.initials}</div>
            <div className="meso-identity-info">
              <div className="meso-identity-name">{athlete.name}</div>
              {athlete.goal && <div className="meso-identity-goal">{athlete.goal}</div>}
            </div>
          </div>
        )}
        {isGroup && group && (
          <div className="meso-identity">
            <div className="meso-group-avatars">
              {group.members.slice(0, 3).map((m, i) => (
                <div key={m.id} className={`meso-group-avatar${i === 0 ? " meso-group-avatar--lead" : ""}`}>
                  {m.initials}
                </div>
              ))}
              {group.member_count > 3 && <div className="meso-group-avatar">{"+" + (group.member_count - 3)}</div>}
            </div>
            <div className="meso-identity-info">
              <div className="meso-identity-name">{group.name}</div>
              <div className="meso-identity-goal">
                {group.member_count + (group.member_count === 1 ? " participant" : " participants")}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="meso-seg" style={{ marginLeft: 4 }}>
        <button
          type="button"
          data-testid="mode-individual-button"
          className={`meso-seg-btn${mode === "individual" ? " is-on" : ""}`}
          onClick={() => onSetMode("individual")}
        >
          Individual
        </button>
        <button
          type="button"
          data-testid="mode-group-button"
          className={`meso-seg-btn${mode === "group" ? " is-on" : ""}`}
          onClick={() => onSetMode("group")}
        >
          Group
        </button>
      </div>

      <div className="meso-flex-spacer" />

      {cycleLabel && (
        <div className="meso-flex meso-cycle-chip">
          <div className="meso-cycle-dot" />
          <span>{cycleLabel}</span>
        </div>
      )}
      <button type="button" data-testid="preview-athlete-button" data-hover="rail" className="meso-btn-rail" onClick={onPreviewAsAthlete}>
        Preview as athlete
      </button>
      {isIndividual && (
        <>
          <a data-testid="review-link" href="/meso/review/" data-hover="rail" className="meso-btn-rail">
            Review changes
          </a>
          <a data-testid="deliver-link" href={deliverHref} data-hover="brighten" className="meso-btn-deliver">
            Deliver
          </a>
        </>
      )}
      {isGroup && <span className="meso-chip-soon">Deliver to all · soon</span>}
    </div>
  );
}
