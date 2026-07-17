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
import { AthleteMeta } from "./components/AthleteMeta";
import { ChatPanel } from "./components/ChatPanel";
import type { DesignerFlags } from "./components/ChatPanel";
import { MesoTable } from "./components/MesoTable";
import { BlockView } from "./components/BlockView";
import type { PeriodStyle } from "./components/BlockView";
import { AthletePreview } from "./components/AthletePreview";

import { useGrid } from "./hooks/useGrid";
import type { Id } from "./hooks/useGrid";
import { useTableReorder } from "./hooks/useTableReorder";
import { useUndoKeyboard } from "./hooks/useUndoKeyboard";
import { useAgentChat } from "./hooks/useAgentChat";
import type { ChatMessage } from "./hooks/useAgentChat";
import { useCoachmarks } from "./hooks/useCoachmarks";

import type { MesoGrid } from "./lib/api";
import { cycleLabelFromGrid, gridToProgram } from "./lib/grid";
import { deliverHref as buildDeliverHref } from "./lib/deliver";

// Issue #455 phase A5: the one-week "week" view is gone — the table shows
// every week as columns at once, so there's no more "viewed week" to land a
// dedicated week view on. Default is now unconditionally "table" (see
// readHydration()/Hydrated below — #meso-grid-data is a required hydration
// gate now, so there's no "grid absent, fall back to week" branch either).
export type ViewMode = "table" | "block" | "athlete";

interface Hydrated {
  planId: Id;
  unit: string;
  csrf: string;
  gridData: MesoGrid;
  initialMessages: ChatMessage[];
  initialResumeUrl: string | null;
  flags: DesignerFlags;
}

// The thread starts with a single orienting greeting. Real agent turns
// append live as the coach sends instructions.
const DEFAULT_GREETING: ChatMessage = {
  id: 1,
  role: "agent",
  text: "Tell me how you'd like to adjust this plan — try “lighten Friday” or “add a deload week.”",
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

  let initialMessages: ChatMessage[] = [DEFAULT_GREETING];
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
    initialMessages,
    initialResumeUrl,
    flags,
  };
}

export function DesignerRoot() {
  const [hydrated] = useState<Hydrated | null>(() => readHydration());
  const [view, setView] = useState<ViewMode>("table");
  // Sidebar (athlete metadata + agent) is visible by default; the coach can
  // collapse it from the top bar to hand the full width to the grid.
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [periodStyle, setPeriodStyle] = useState<PeriodStyle>("timeline");
  const [checks, setChecks] = useState<Record<string, boolean>>({});

  const planId: Id = hydrated?.planId ?? "";
  const csrf = hydrated?.csrf ?? "";
  const unit = hydrated?.unit ?? "kg";

  // useGrid is the SOLE data owner (issue #455 phase A5 — the one-week
  // usePlanData sibling is gone). Owns grid/history and every verb that
  // mutates them; the top bar / left rail / block view / athlete preview all
  // re-source off `gridState.grid` (plan/athlete/phases/weeks/days),
  // additive fields serialize_mesocycle_grid now carries for exactly this
  // reason (see serializers.py).
  const gridState = useGrid({ planId, csrf, initialGrid: hydrated?.gridData ?? null });

  // The global Ctrl/Cmd+Z window shortcut now always routes to the grid's
  // own undo/redo — there's only one undo/redo owner left, so the
  // view-conditional routing the retired useUndoRedo needed is gone too.
  useUndoKeyboard(gridState.undo, gridState.redo);

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
        view={view}
        onSelectView={selectView}
        cycleLabel={cycleLabel}
        deliverHref={deliverHref}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((open) => !open)}
      />

      <div className="meso-designer-body">
        <div className={`meso-sidebar${sidebarOpen ? "" : " meso-sidebar--collapsed"}`}>
          <AthleteMeta athlete={grid?.athlete ?? null} />

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
        </div>

        <div className="meso-canvas">
          <div className="meso-canvas-body">
            {view === "table" && (
              <MesoTable
                grid={gridState.grid}
                history={gridState.history}
                busy={gridState.busy}
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
    </div>
  );
}
