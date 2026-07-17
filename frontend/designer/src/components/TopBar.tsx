// TopBar (CONTRACT.md "TopBar") — ported 1:1 from designer.html's top bar
// (lines ~42-98): brand/back-link, athlete identity chip, cycle label,
// preview/review/deliver actions. (The individual/group mode segmented
// control went with the group subsystem — the designer is single-mode now.)
import type { AthleteIdentity } from "../lib/api";

export interface TopBarProps {
  athlete: AthleteIdentity | null;
  cycleLabel: string;
  onPreviewAsAthlete(): void;
  deliverHref: string;
}

export function TopBar(props: TopBarProps) {
  const { athlete, cycleLabel, onPreviewAsAthlete, deliverHref } = props;

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
        {athlete && (
          <div className="meso-identity">
            <div className="meso-identity-avatar">{athlete.initials}</div>
            <div className="meso-identity-info">
              <div className="meso-identity-name">{athlete.name}</div>
              {athlete.goal && <div className="meso-identity-goal">{athlete.goal}</div>}
            </div>
          </div>
        )}
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
      <a data-testid="review-link" href="/meso/review/" data-hover="rail" className="meso-btn-rail">
        Review changes
      </a>
      <a data-testid="deliver-link" href={deliverHref} data-hover="brighten" className="meso-btn-deliver">
        Deliver
      </a>
    </div>
  );
}
