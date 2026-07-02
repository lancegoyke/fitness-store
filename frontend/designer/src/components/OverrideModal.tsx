// OverrideModal (CONTRACT.md "OverrideModal") — ported 1:1 from
// designer.html's per-athlete override editor (lines ~605-662). Rendered
// only when override !== null. Backdrop click + Escape both call onClose
// (which internally guards on `saving`, per useOverrideEditor).
import { useEffect } from "react";
import { numeric } from "../lib/grid";
import type { OverrideEditorState } from "../hooks/useOverrideEditor";
import type { OverrideDraft } from "../lib/override";

export interface OverrideModalProps {
  override: OverrideEditorState | null;
  overrideHasExisting: boolean;
  unit: string;
  onSelectMember(memberId: string): void;
  onUpdateDraft(patch: Partial<OverrideDraft>): void;
  onClose(): void;
  onSave(): void;
  onClear(): void;
}

export function OverrideModal(props: OverrideModalProps) {
  const { override, overrideHasExisting, unit, onSelectMember, onUpdateDraft, onClose, onSave, onClear } = props;

  useEffect(() => {
    if (!override) return;
    function handler(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [override, onClose]);

  if (!override) return null;
  const { ex, members, memberId, draft, saving, error } = override;

  return (
    <div
      className="meso-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="meso-modal">
        <div className="meso-modal-header">
          <div className="meso-modal-eyebrow">Per-athlete adjust</div>
          <div className="meso-modal-title">{ex.name}</div>
          <div className="meso-modal-meta meso-mono">
            shared {ex.sets}×{ex.reps}
            {numeric(ex.load) && <span>{" · " + ex.load + (ex.load_type === "pct" ? "%" : " " + unit)}</span>}
          </div>
        </div>

        <div className="meso-modal-section">
          <p className="meso-rail-label">Athlete</p>
          <div className="meso-flex meso-wrap">
            {members.map((m) => (
              <button
                key={m.id}
                type="button"
                data-testid={`override-member-${m.id}`}
                className={`meso-member-btn${m.id === memberId ? " is-selected" : ""}`}
                onClick={() => onSelectMember(m.id)}
              >
                <span>{m.name}</span>
                {(ex.adjusts || []).some((a) => a.id === m.id) && <span className="meso-member-dot" />}
              </button>
            ))}
          </div>
        </div>

        <div className="meso-modal-fields">
          <div className="meso-field meso-field--full">
            <label className="meso-field-label">Swap exercise</label>
            <input
              data-testid="override-swap-input"
              className="meso-field-input"
              placeholder={ex.name}
              value={draft.swap}
              onChange={(e) => onUpdateDraft({ swap: e.target.value })}
            />
          </div>
          <div className="meso-field">
            <label className="meso-field-label">Load %</label>
            <input
              data-testid="override-load-pct-input"
              className="meso-field-input meso-mono"
              inputMode="numeric"
              placeholder="100"
              value={draft.load_pct}
              onChange={(e) => onUpdateDraft({ load_pct: e.target.value })}
            />
          </div>
          <div className="meso-field">
            <label className="meso-field-label">Sets × Reps</label>
            <div className="meso-flex meso-field-pair">
              <input
                data-testid="override-sets-input"
                className="meso-field-input meso-mono meso-field-input--center"
                placeholder={ex.sets}
                value={draft.sets}
                onChange={(e) => onUpdateDraft({ sets: e.target.value })}
              />
              <span>×</span>
              <input
                data-testid="override-reps-input"
                className="meso-field-input meso-mono meso-field-input--center"
                placeholder={ex.reps}
                value={draft.reps}
                onChange={(e) => onUpdateDraft({ reps: e.target.value })}
              />
            </div>
          </div>
          <div className="meso-field meso-field--full">
            <label className="meso-field-label">Note</label>
            <input
              data-testid="override-note-input"
              className="meso-field-input"
              placeholder="—"
              value={draft.note}
              onChange={(e) => onUpdateDraft({ note: e.target.value })}
            />
          </div>
        </div>

        <div className="meso-modal-error-slot">
          {error && (
            <div data-testid="override-error" className="meso-modal-error">
              {error}
            </div>
          )}
        </div>

        <div className="meso-modal-footer">
          {overrideHasExisting && (
            <button type="button" data-testid="override-clear-button" data-hover="rail" className="meso-clear-btn" disabled={saving} onClick={onClear}>
              Clear adjust
            </button>
          )}
          <div className="meso-flex-spacer" />
          <button
            type="button"
            data-testid="override-cancel-button"
            data-hover="rail"
            className="meso-cancel-btn-outline"
            disabled={saving}
            onClick={onClose}
          >
            Cancel
          </button>
          <button type="button" data-testid="override-save-button" data-hover="brighten" className="meso-save-btn" disabled={saving} onClick={onSave}>
            Save adjust
          </button>
        </div>
      </div>
    </div>
  );
}
