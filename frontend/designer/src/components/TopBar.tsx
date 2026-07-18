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
  // null when the viewed block has no live week to deliver (the default
  // grid can open on an empty first block while a later block has weeks —
  // docs/meso/remove-current-week-plan.md §4b) — renders the control inert
  // rather than a link that would silently target a different block.
  deliverHref: string | null;
  sidebarOpen: boolean;
  onToggleSidebar(): void;
  // Undo/redo live here (global, Ctrl+Z-backed editor actions) rather than in
  // the grid's own toolbar. data-grid-restore is preserved on each button so
  // clicking it returns focus to the grid's anchor cell (see useTableNav).
  canUndo: boolean;
  canRedo: boolean;
  undoLabel: string | null;
  redoLabel: string | null;
  onUndo(): void;
  onRedo(): void;
}

const VIEW_TABS: { id: ViewMode; label: string }[] = [
  { id: "table", label: "Table" },
  { id: "block", label: "Periodization" },
  { id: "athlete", label: "Athlete view" },
];

export function TopBar({
  view,
  onSelectView,
  cycleLabel,
  deliverHref,
  sidebarOpen,
  onToggleSidebar,
  canUndo,
  canRedo,
  undoLabel,
  redoLabel,
  onUndo,
  onRedo,
}: TopBarProps) {
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
      <div className="meso-topbar-iconbtns">
        <button
          type="button"
          data-testid="grid-undo"
          data-grid-restore=""
          className="meso-topbar-iconbtn"
          disabled={!canUndo}
          aria-label="Undo"
          title={undoLabel ? "Undo: " + undoLabel : "Undo"}
          onClick={onUndo}
        >
          ↺
        </button>
        <button
          type="button"
          data-testid="grid-redo"
          data-grid-restore=""
          className="meso-topbar-iconbtn"
          disabled={!canRedo}
          aria-label="Redo"
          title={redoLabel ? "Redo: " + redoLabel : "Redo"}
          onClick={onRedo}
        >
          ↻
        </button>
      </div>
      <a data-testid="review-link" href="/meso/review/" data-hover="rail" className="meso-btn-rail">
        Review changes
      </a>
      {deliverHref ? (
        <a data-testid="deliver-link" href={deliverHref} data-hover="brighten" className="meso-btn-deliver">
          Deliver
        </a>
      ) : (
        // No live week in the viewed block — nothing to deliver. Inert: no
        // href (so no navigation, with or without JS) + aria-disabled,
        // matching the visual treatment .meso-topbar-iconbtn:disabled uses
        // for the undo/redo buttons above (opacity + default cursor), the
        // only other disabled-control convention in this island.
        <a
          data-testid="deliver-link"
          aria-disabled="true"
          className="meso-btn-deliver meso-btn-deliver--disabled"
          title="No live week in this block to deliver"
        >
          Deliver
        </a>
      )}
    </div>
  );
}
