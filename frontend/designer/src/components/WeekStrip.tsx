// WeekStrip (CONTRACT.md "WeekStrip") — ported 1:1 from designer.html's week
// switcher (lines ~343-372): week chips, add week, make-current, remove-week
// arm/confirm/cancel, undo/redo. Rendered only when weeks.length > 0 (the
// source's `x-show="live && weeks.length"`; `live` is dropped per Non-goals).
import type { HistoryState, Week } from "../lib/api";
import type { Id, PendingDelete } from "../hooks/usePlanData";

export interface WeekStripProps {
  weeks: Week[];
  viewedWeekId: Id | null;
  viewedIsCurrent: boolean;
  pendingDelete: PendingDelete | null;
  deleting: boolean;
  history: HistoryState;
  undoing: boolean;
  onSwitchWeek(weekId: Id): void;
  onAddWeek(): void;
  onMakeCurrent(weekId: Id): void;
  onRequestRemoveWeek(weekId: Id): void;
  onCancelPendingDelete(): void;
  onConfirmPendingDelete(): void;
  // Optional: WeekGrid's own contract doesn't list these (see WeekGrid.test.tsx's
  // "contract gap" note) — defaulted to a no-op here so WeekGrid can forward them
  // straight through even when its own caller hasn't wired them yet.
  onUndo?(): void;
  onRedo?(): void;
}

export function WeekStrip(props: WeekStripProps) {
  const {
    weeks,
    viewedWeekId,
    viewedIsCurrent,
    pendingDelete,
    deleting,
    history,
    undoing,
    onSwitchWeek,
    onAddWeek,
    onMakeCurrent,
    onRequestRemoveWeek,
    onCancelPendingDelete,
    onConfirmPendingDelete,
    onUndo,
    onRedo,
  } = props;

  if (!weeks.length) return null;

  const weekArmed =
    !!pendingDelete && pendingDelete.type === "week" && pendingDelete.weekId === viewedWeekId;

  return (
    <div className="meso-flex meso-week-strip">
      {weeks.map((w) => (
        <button
          key={w.id}
          type="button"
          data-testid={`week-chip-${w.id}`}
          data-grid-restore=""
          data-hover="rail"
          className={`meso-week-chip${w.id === viewedWeekId ? " is-viewed" : ""}`}
          title={w.current ? "Live week — delivery sends this one" : "View " + w.label}
          onClick={() => onSwitchWeek(w.id)}
        >
          <span>{w.label}</span>
          {w.current && <span className="meso-week-chip-dot" title="Live week" />}
        </button>
      ))}
      <button
        type="button"
        data-testid="add-week-button"
          data-grid-restore=""
        data-hover="add"
        className="meso-week-strip-btn meso-week-strip-btn--dashed"
        onClick={onAddWeek}
      >
        + Add week
      </button>
      {!viewedIsCurrent && viewedWeekId != null && (
        <button
          type="button"
          data-testid="make-current-button"
          data-grid-restore=""
          data-hover="brighten"
          className="meso-week-strip-btn meso-week-strip-btn--accent"
          title="Make this the live week — delivery will send it"
          onClick={() => onMakeCurrent(viewedWeekId)}
        >
          Make current
        </button>
      )}
      {!viewedIsCurrent && !weekArmed && viewedWeekId != null && (
        <button
          type="button"
          data-testid="remove-week-button"
          data-grid-restore=""
          data-hover="rail"
          className="meso-week-strip-btn"
          disabled={deleting}
          aria-label="Remove this week"
          title="Remove this week"
          onClick={() => onRequestRemoveWeek(viewedWeekId)}
        >
          Remove week
        </button>
      )}
      {!viewedIsCurrent && weekArmed && (
        <span className="meso-week-strip-confirm">
          <button
            type="button"
            data-testid="confirm-remove-week-button"
          data-grid-restore=""
            data-hover="brighten"
            className="meso-week-strip-btn meso-week-strip-btn--confirm"
            disabled={deleting}
            aria-label="Confirm remove week"
            onClick={onConfirmPendingDelete}
          >
            Confirm?
          </button>
          <button
            type="button"
            data-testid="cancel-remove-week-button"
          data-grid-restore=""
            data-hover="rail"
            className="meso-week-strip-btn"
            disabled={deleting}
            aria-label="Cancel remove week"
            onClick={onCancelPendingDelete}
          >
            Cancel
          </button>
        </span>
      )}
      <span className="meso-undo-redo-group">
        <button
          type="button"
          data-testid="undo-button"
          data-grid-restore=""
          data-hover="rail"
          className="meso-week-strip-btn"
          disabled={undoing || !history.can_undo}
          aria-label="Undo"
          title={history.undo_label ? "Undo: " + history.undo_label : "Undo"}
          onClick={() => onUndo?.()}
        >
          ↺ Undo
        </button>
        <button
          type="button"
          data-testid="redo-button"
          data-grid-restore=""
          data-hover="rail"
          className="meso-week-strip-btn"
          disabled={undoing || !history.can_redo}
          aria-label="Redo"
          title={history.redo_label ? "Redo: " + history.redo_label : "Redo"}
          onClick={() => onRedo?.()}
        >
          Redo ↻
        </button>
      </span>
    </div>
  );
}
