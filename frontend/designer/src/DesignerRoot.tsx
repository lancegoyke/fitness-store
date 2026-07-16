// DesignerRoot (CONTRACT.md "DesignerRoot") — the Phase 2 PR B island root.
//
// Issue #455 phase A5: the one-week designer (WeekStrip/WeekGrid/DayCard/
// ExerciseRow + usePlanData/useAutosave/useDeletes/useUndoRedo/useReorder/
// useOneRmEditor) is retired — the P1 multi-week table (useGrid/MesoTable)
// is now the ONLY data owner. Hydrates once from the #meso-grid-data /
// #meso-chat-thread / #meso-csrf / #meso-designer-flags json_script
// elements: #meso-grid-data is now the hydration GATE (a plan with no
// mesocycle block at all — documented "shouldn't happen post-scaffold" —
// renders a blank island rather than a degraded one-week grid; see
// views.py's MesoDesignerView.get_context_data), not just an optional
// enhancement layered onto a separate #meso-plan-data payload.
import { useCallback, useState } from "react";
import "./designer.css";

import { TopBar } from "./components/TopBar";
import type { DesignerMode } from "./components/TopBar";
import { LeftRail } from "./components/LeftRail";
import { ChatPanel } from "./components/ChatPanel";
import type { DesignerFlags } from "./components/ChatPanel";
import { MesoTable } from "./components/MesoTable";
import { BlockView } from "./components/BlockView";
import type { PeriodStyle } from "./components/BlockView";
import { AthletePreview } from "./components/AthletePreview";
import { OverrideModal } from "./components/OverrideModal";

import { useOverrideEditor } from "./hooks/useOverrideEditor";
import { useGrid } from "./hooks/useGrid";
import type { Id } from "./hooks/useGrid";
import { useTableReorder } from "./hooks/useTableReorder";
import { useUndoKeyboard } from "./hooks/useUndoKeyboard";
import { useAgentChat } from "./hooks/useAgentChat";
import type { ChatMessage } from "./hooks/useAgentChat";
import { useCoachmarks } from "./hooks/useCoachmarks";

import type { Exercise, GridCell, GridRow, MesoGrid } from "./lib/api";
import { cycleLabelFromGrid, gridToProgram } from "./lib/grid";
import { deliverHref as buildDeliverHref } from "./lib/deliver";

// Issue #455 phase A5: the one-week "week" view is gone — the table shows
// every week as columns at once, so there's no more "viewed week" to land a
// dedicated week view on. Default is now unconditionally "table" (see
// readHydration()/Hydrated below — #meso-grid-data is a required hydration
// gate now, so there's no "grid absent, fall back to week" branch either).
type ViewMode = "table" | "block" | "athlete";

interface Hydrated {
  planId: Id;
  unit: string;
  csrf: string;
  gridData: MesoGrid;
  initialMode: DesignerMode;
  initialMessages: ChatMessage[];
  initialResumeUrl: string | null;
  flags: DesignerFlags;
}

// The thread starts with a single orienting greeting — a group-aware opening
// line when the plan has a group, ported from init()'s override. Real agent
// turns append live as the coach sends instructions.
const DEFAULT_GREETING: ChatMessage = {
  id: 1,
  role: "agent",
  text: "Tell me how you'd like to adjust this plan — try “lighten Friday” or “add a deload week.”",
};
const GROUP_GREETING: ChatMessage = {
  id: 1,
  role: "agent",
  text:
    "This is the group's shared program — every member trains off it. Ask me to change it for the whole group, or to " +
    "adjust one athlete (a swap, a load %, or a volume tweak just for them). I propose changes for you to review and " +
    "honor every member's contraindications.",
};

const DEFAULT_FLAGS: DesignerFlags = {
  is_sandbox: false,
  can_use_agent: false,
  agent_allowance: { metered: false, allowance: 0, remaining: null, can_use: false, tier: "unlimited" },
  signup_url: "/meso/sandbox/signup/",
  price_summary: "",
};

/** Reads the hydration json_script elements once. #meso-grid-data is now the
 * required gate (absent/unparseable -> null, the no-op-without-a-plan guard
 * — a plan with no mesocycle block at all is a documented "shouldn't happen
 * post-scaffold" edge case, issue #455 phase A5); #meso-chat-thread /
 * #meso-csrf / #meso-designer-flags are each independently tolerant of
 * absence/parse errors, same as before. */
function readHydration(): Hydrated | null {
  const gridEl = document.getElementById("meso-grid-data");
  if (!gridEl) return null;

  let gridData: MesoGrid;
  try {
    gridData = JSON.parse(gridEl.textContent || "") as MesoGrid;
  } catch (err) {
    console.error("Could not parse grid data", err);
    return null;
  }

  const group = gridData.group ?? null;

  let initialMessages: ChatMessage[] = group ? [GROUP_GREETING] : [DEFAULT_GREETING];
  let initialResumeUrl: string | null = null;
  const threadEl = document.getElementById("meso-chat-thread");
  if (threadEl) {
    try {
      const thread = JSON.parse(threadEl.textContent || "") as (ChatMessage & { pollUrl?: string })[];
      if (Array.isArray(thread) && thread.length) {
        const messages = [...thread];
        const last = messages[messages.length - 1];
        if (last && last.pollUrl) {
          messages.pop();
          initialResumeUrl = last.pollUrl;
        }
        initialMessages = messages;
      }
    } catch (err) {
      console.error("Could not parse chat thread", err);
    }
  }

  const csrfEl = document.getElementById("meso-csrf") as HTMLElement | null;
  const csrf = csrfEl?.dataset.token ?? "";

  let flags = DEFAULT_FLAGS;
  const flagsEl = document.getElementById("meso-designer-flags");
  if (flagsEl) {
    try {
      flags = JSON.parse(flagsEl.textContent || "") as DesignerFlags;
    } catch (err) {
      console.error("Could not parse designer flags", err);
    }
  }

  return {
    planId: gridData.mesocycle.plan_id,
    unit: gridData.plan?.unit || "kg",
    csrf,
    gridData,
    initialMode: group ? "group" : "individual",
    initialMessages,
    initialResumeUrl,
    flags,
  };
}

/** The override editor's `openOverride(ex)` takes an `Exercise`, but the
 * multi-week table works in (GridRow, GridCell) pairs — each cell IS a
 * Prescription. Synthesize the Exercise the editor needs from that pair: the
 * cell's freeform text under the row's block name, carrying `adj`/`adjusts`
 * so the modal preselects the adjusted member and seeds their draft (from
 * the member's stored adjust only — Phase 2a: the base cell has no
 * structured sets/reps left to seed from). `id` is the cell's
 * `prescription_id` — the same id the override reply patches back. */
function synthesizeCellExercise(row: GridRow, cell: GridCell): Exercise {
  return {
    id: cell.prescription_id,
    name: row.name,
    text: cell.text,
    adj: cell.adj ?? null,
    adjusts: cell.adjusts ?? [],
  };
}

export function DesignerRoot() {
  const [hydrated] = useState<Hydrated | null>(() => readHydration());
  const [mode, setMode] = useState<DesignerMode>(hydrated?.initialMode ?? "individual");
  const [view, setView] = useState<ViewMode>("table");
  const [periodStyle, setPeriodStyle] = useState<PeriodStyle>("timeline");
  const [checks, setChecks] = useState<Record<string, boolean>>({});

  const planId: Id = hydrated?.planId ?? "";
  const csrf = hydrated?.csrf ?? "";
  const unit = hydrated?.unit ?? "kg";
  const isIndividual = mode === "individual";
  const isGroup = mode === "group";

  // useGrid is the SOLE data owner (issue #455 phase A5 — the one-week
  // usePlanData sibling is gone). Owns grid/history and every verb that
  // mutates them; the top bar / left rail / block view / athlete preview all
  // re-source off `gridState.grid` (plan/group/athlete/phases/weeks/days),
  // additive fields serialize_mesocycle_grid now carries for exactly this
  // reason (see serializers.py).
  const gridState = useGrid({ planId, csrf, initialGrid: hydrated?.gridData ?? null });

  // The global Ctrl/Cmd+Z window shortcut now always routes to the grid's
  // own undo/redo — there's only one undo/redo owner left, so the
  // view-conditional routing the retired useUndoRedo needed is gone too.
  useUndoKeyboard(gridState.undo, gridState.redo);

  // P5 group: the table's own per-cell override editor — the ONLY instance
  // now (the retired planData-scoped sibling is gone). Its save/clear reply
  // repaints the grid cell (patchCellAdj) and adopts the grid's own undo
  // history (adoptGridHistory).
  const overrideEditor = useOverrideEditor({
    planId,
    csrf,
    group: gridState.grid?.group ?? null,
    adoptHistory: gridState.adoptGridHistory,
    patchExercise: gridState.patchCellAdj,
  });
  // Issue #455 phase A2 (drag reordering): the table's own pure drag-event
  // translator — wired to gridState's structural verbs
  // (reorderExercises/reorderDays/moveExerciseToDay).
  const tableReorder = useTableReorder({
    grid: gridState.grid,
    reorderRow: gridState.reorderExercises,
    reorderDay: gridState.reorderDays,
  });
  const agentChat = useAgentChat({
    planId,
    csrf,
    initialMessages: hydrated?.initialMessages ?? [DEFAULT_GREETING],
    initialResumeUrl: hydrated?.initialResumeUrl ?? null,
  });
  const coachmarks = useCoachmarks();

  const selectView = useCallback((v: ViewMode) => setView(v), []);

  if (!hydrated) return null;

  const flags = hydrated.flags;

  const grid = gridState.grid;
  const gridCurrentWeekId = grid ? grid.weeks.find((w) => w.current)?.id ?? grid.weeks[0]?.id ?? null : null;
  const deliverHref = buildDeliverHref(planId, gridCurrentWeekId);
  const cycleLabel = cycleLabelFromGrid(grid?.phases ?? [], grid?.weeks ?? []);
  const athleteProgram = grid ? gridToProgram(grid) : [];

  return (
    <div className="meso-designer-root">
      <TopBar
        mode={mode}
        onSetMode={setMode}
        isIndividual={isIndividual}
        isGroup={isGroup}
        athlete={grid?.athlete ?? null}
        group={grid?.group ?? null}
        cycleLabel={cycleLabel}
        onPreviewAsAthlete={() => selectView("athlete")}
        deliverHref={deliverHref}
      />

      <div className="meso-designer-body">
        <LeftRail
          isIndividual={isIndividual}
          isGroup={isGroup}
          athlete={grid?.athlete ?? null}
          group={grid?.group ?? null}
          phases={grid?.phases ?? []}
          onOpenBlockView={() => selectView("block")}
        />

        <ChatPanel
          messages={agentChat.messages}
          agentTyping={agentChat.agentTyping}
          chips={agentChat.chips}
          inputText={agentChat.inputText}
          onInputChange={agentChat.setInputText}
          onInputKey={agentChat.onInputKey}
          onSend={agentChat.onSend}
          onChip={agentChat.onChip}
          threadRef={agentChat.threadRef}
          flags={flags}
        />

        <div className="meso-canvas">
          <div className="meso-canvas-header">
            <div className="meso-seg">
              <button type="button" className={`meso-seg-btn meso-seg-btn--v${view === "table" ? " is-on" : ""}`} onClick={() => selectView("table")}>
                Table
              </button>
              <button type="button" className={`meso-seg-btn meso-seg-btn--v${view === "block" ? " is-on" : ""}`} onClick={() => selectView("block")}>
                Periodization
              </button>
              <button
                type="button"
                className={`meso-seg-btn meso-seg-btn--v${view === "athlete" ? " is-on" : ""}`}
                onClick={() => selectView("athlete")}
              >
                Athlete view
              </button>
            </div>
            <div className="meso-flex-spacer" />
          </div>

          <div className="meso-canvas-body">
            {view === "table" && (
              <MesoTable
                grid={gridState.grid}
                history={gridState.history}
                busy={gridState.busy}
                group={grid?.group ?? null}
                onOpenOverride={(row, cell) => overrideEditor.openOverride(synthesizeCellExercise(row, cell))}
                onPatchCell={gridState.patchCell}
                onWriteCellLine={gridState.writeCellLine}
                onPatchRowColumns={gridState.patchRowColumns}
                onRenameExercise={gridState.renameExercise}
                onMoveExerciseToDay={gridState.moveExerciseToDay}
                onAddExercise={gridState.addExercise}
                onRemoveExercise={gridState.removeExercise}
                onAddDay={gridState.addDay}
                onRemoveDay={gridState.removeDay}
                onAddWeek={gridState.addWeek}
                onRemoveWeek={gridState.removeWeek}
                onSetCurrentWeek={gridState.setCurrentWeek}
                onUndo={gridState.undo}
                onRedo={gridState.redo}
                onSkipCell={gridState.skipCell}
                onFillAcrossWeeks={gridState.fillAcrossWeeks}
                onAddExerciseThisWeek={gridState.addExerciseThisWeek}
                onDragEnd={tableReorder.onDragEnd}
                coachmarkVisible={coachmarks.coachmarkVisible}
                dismissCoachmark={coachmarks.dismissCoachmark}
              />
            )}

            {view === "block" && (
              <BlockView
                phases={grid?.phases ?? []}
                weeks={grid?.weeks ?? []}
                periodStyle={periodStyle}
                onSetPeriodStyle={setPeriodStyle}
                // Issue #455 phase A5 product-behavior change: there is no
                // more one-week "landing" view to preview a week into — the
                // table already shows every week as columns at once, so a
                // timeline bar click switches to the table view instead
                // (ignoring the clicked weekId; scrolling that week's column
                // into view is a nice-to-have left out of scope here).
                onSwitchWeek={() => selectView("table")}
              />
            )}

            {view === "athlete" && (
              <AthletePreview
                program={athleteProgram}
                unit={unit}
                checks={checks}
                onToggleCheck={(k) => setChecks((prev) => ({ ...prev, [k]: !prev[k] }))}
                coachmarkVisible={coachmarks.coachmarkVisible}
                dismissCoachmark={coachmarks.dismissCoachmark}
              />
            )}
          </div>
        </div>
      </div>

      <OverrideModal
        override={overrideEditor.override}
        overrideHasExisting={overrideEditor.overrideHasExisting}
        onSelectMember={overrideEditor.selectOverrideMember}
        onUpdateDraft={overrideEditor.updateDraft}
        onClose={overrideEditor.closeOverride}
        onSave={overrideEditor.saveOverride}
        onClear={overrideEditor.clearOverride}
      />
    </div>
  );
}
