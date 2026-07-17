// TopBar (CONTRACT.md "TopBar") — brand/back-link, the sidebar toggle, the
// view segmented control (Table / Periodization / Athlete view), the cycle
// label chip, and the review/deliver actions. designer-simplify: the athlete
// identity chip moved to the sidebar's AthleteMeta (it was a duplicate of the
// rail's), the standalone "Preview as athlete" button was dropped (the
// segmented "Athlete view" is the one canonical switch), and the canvas's own
// 49px segmented-control band folded up here.
import type { ViewMode } from "../DesignerRoot";

export interface TopBarProps {
  view: ViewMode;
  onSelectView(view: ViewMode): void;
  cycleLabel: string;
  deliverHref: string;
  sidebarOpen: boolean;
  onToggleSidebar(): void;
}

const VIEW_TABS: { id: ViewMode; label: string }[] = [
  { id: "table", label: "Table" },
  { id: "block", label: "Periodization" },
  { id: "athlete", label: "Athlete view" },
];

export function TopBar({ view, onSelectView, cycleLabel, deliverHref, sidebarOpen, onToggleSidebar }: TopBarProps) {
  return (
    <div className="meso-topbar">
      <a href="/meso/" title="Back to roster" className="meso-topbar-brand">
        <span className="meso-topbar-mark">
          <span className="meso-topbar-mark-glyph" />
        </span>
        <span className="meso-topbar-title">Meso</span>
      </a>
      <div className="meso-topbar-divider" />

      <button
        type="button"
        data-testid="sidebar-toggle"
        className="meso-sidebar-toggle"
        aria-pressed={sidebarOpen}
        aria-label={sidebarOpen ? "Hide athlete & agent panel" : "Show athlete & agent panel"}
        title={sidebarOpen ? "Hide panel" : "Show panel"}
        onClick={onToggleSidebar}
      >
        {sidebarOpen ? "‹" : "›"}
      </button>

      <div className="meso-seg" role="tablist" aria-label="Designer view">
        {VIEW_TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={view === t.id}
            data-testid={`view-tab-${t.id}`}
            className={`meso-seg-btn${view === t.id ? " is-on" : ""}`}
            onClick={() => onSelectView(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="meso-flex-spacer" />

      {cycleLabel && (
        <div className="meso-flex meso-cycle-chip">
          <div className="meso-cycle-dot" />
          <span>{cycleLabel}</span>
        </div>
      )}
      <a data-testid="review-link" href="/meso/review/" data-hover="rail" className="meso-btn-rail">
        Review changes
      </a>
      <a data-testid="deliver-link" href={deliverHref} data-hover="brighten" className="meso-btn-deliver">
        Deliver
      </a>
    </div>
  );
}
