// Placeholder mount target for the Phase 2 island port
// (docs/meso/designer-framework-plan.md, Phase 2). This PR (PR A) only proves
// the Vite + React 19 + TypeScript build and test pipeline end-to-end; PR B
// replaces this with the real ported designer UI (TopBar, WeekStrip,
// WeekGrid, ChatPanel, ...) and wires designer.html to mount it.
export function DesignerRoot() {
  return <div data-testid="designer-root-placeholder">Designer island scaffold</div>;
}
