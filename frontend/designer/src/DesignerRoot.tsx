// DesignerRoot (CONTRACT.md "DesignerRoot") — the Phase 2 PR B island root.
// Hydrates once from the #meso-plan-data / #meso-chat-thread / #meso-csrf /
// #meso-designer-flags json_script elements the same way designer.html's
// init()/hydrateThread() did, composes every hook, and renders the tree.
import { useCallback, useState } from "react";
import "./designer.css";

import { TopBar } from "./components/TopBar";
import type { DesignerMode } from "./components/TopBar";
import { LeftRail } from "./components/LeftRail";
import { ChatPanel } from "./components/ChatPanel";
import type { DesignerFlags } from "./components/ChatPanel";
import { WeekGrid } from "./components/WeekGrid";
import { MesoTable } from "./components/MesoTable";
import { BlockView } from "./components/BlockView";
import type { PeriodStyle } from "./components/BlockView";
import { AthletePreview } from "./components/AthletePreview";
import { OverrideModal } from "./components/OverrideModal";

import { usePlanData } from "./hooks/usePlanData";
import type { Id } from "./hooks/usePlanData";
import { useAutosave } from "./hooks/useAutosave";
import { useDeletes } from "./hooks/useDeletes";
import { useUndoRedo } from "./hooks/useUndoRedo";
import { useOverrideEditor } from "./hooks/useOverrideEditor";
import { useOneRmEditor } from "./hooks/useOneRmEditor";
import { useReorder } from "./hooks/useReorder";
import { useGrid } from "./hooks/useGrid";
import { useTableReorder } from "./hooks/useTableReorder";
import { useAgentChat } from "./hooks/useAgentChat";
import type { ChatMessage } from "./hooks/useAgentChat";
import { useCoachmarks } from "./hooks/useCoachmarks";

import type {
  AthleteIdentity,
  Day,
  Exercise,
  GridCell,
  GridRow,
  GroupIdentity,
  HistoryState,
  MesoGrid,
  Phase,
  PlanSummary,
  Week,
} from "./lib/api";
import { deliverHref as buildDeliverHref } from "./lib/deliver";

// P1 (multi-week table) adds "table" as the DEFAULT view when a mesocycle
// grid is hydrated — see readHydration()'s gridData/initialView below. The
// pre-existing "week"/"block"/"athlete" branches are untouched (transitional
// fallback — see MesoTable.tsx's header).
type ViewMode = "week" | "block" | "athlete" | "table";

interface HydratedPlanPayload {
  plan: PlanSummary;
  group: GroupIdentity | null;
  athlete: AthleteIdentity | null;
  program: Day[];
  weeks: Week[];
  phases: Phase[];
  viewing: Id | null;
  history?: HistoryState;
}

interface Hydrated {
  planId: Id;
  unit: string;
  csrf: string;
  group: GroupIdentity | null;
  athlete: AthleteIdentity | null;
  program: Day[];
  weeks: Week[];
  phases: Phase[];
  viewing: Id | null;
  history?: HistoryState;
  initialMode: DesignerMode;
  initialMessages: ChatMessage[];
  initialResumeUrl: string | null;
  flags: DesignerFlags;
  gridData: MesoGrid | null;
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

/** Reads the four hydration json_script elements once, mirroring meso.js's
 * init()/hydrateThread(). Absent/unparseable plan data -> null (the
 * no-op-without-a-plan guard ported from init()'s early return). */
function readHydration(): Hydrated | null {
  const dataEl = document.getElementById("meso-plan-data");
  if (!dataEl) return null;

  let data: HydratedPlanPayload;
  try {
    data = JSON.parse(dataEl.textContent || "") as HydratedPlanPayload;
  } catch (err) {
    console.error("Could not parse plan data", err);
    return null;
  }

  const group = data.group ?? null;
  const athlete = data.athlete ?? null;

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

  // P1 (multi-week table): optional — absent on plans without a mesocycle
  // grid yet, same tolerate-absence/parse-error handling as every other
  // hydration element above.
  let gridData: MesoGrid | null = null;
  const gridEl = document.getElementById("meso-grid-data");
  if (gridEl) {
    try {
      gridData = JSON.parse(gridEl.textContent || "") as MesoGrid;
    } catch (err) {
      console.error("Could not parse grid data", err);
    }
  }

  return {
    planId: data.plan.id,
    unit: data.plan.unit || "kg",
    csrf,
    group,
    athlete,
    program: data.program,
    weeks: data.weeks,
    phases: data.phases,
    viewing: data.viewing ?? null,
    history: data.history,
    initialMode: group ? "group" : "individual",
    initialMessages,
    initialResumeUrl,
    flags,
    gridData,
  };
}

/** P5 group: the override editor's `openOverride(ex)` takes an `Exercise`, but
 * the multi-week table works in (GridRow, GridCell) pairs — each cell IS a
 * Prescription. Synthesize the Exercise the editor needs from that pair: the
 * cell's own numbers under the row's block name, carrying `adj`/`adjusts` so
 * the modal preselects the adjusted member and seeds their draft. `id` is the
 * cell's `prescription_id` — the same id the override reply patches back. */
function synthesizeCellExercise(row: GridRow, cell: GridCell): Exercise {
  return {
    id: cell.prescription_id,
    name: row.name,
    sets: cell.sets,
    reps: cell.reps,
    load: cell.load,
    load_type: cell.load_type,
    rpe: cell.rpe,
    note: cell.note,
    adj: cell.adj ?? null,
    adjusts: cell.adjusts ?? [],
  };
}

export function DesignerRoot() {
  const [hydrated] = useState<Hydrated | null>(() => readHydration());
  const [mode, setMode] = useState<DesignerMode>(hydrated?.initialMode ?? "individual");
  // P1: default to the table view once a mesocycle grid is hydrated (it
  // becomes the coach's primary editing surface); falls back to today's
  // "week" default on plans with no grid yet.
  const [view, setView] = useState<ViewMode>(hydrated?.gridData ? "table" : "week");
  const [periodStyle, setPeriodStyle] = useState<PeriodStyle>("timeline");
  const [checks, setChecks] = useState<Record<string, boolean>>({});

  const planId: Id = hydrated?.planId ?? "";
  const csrf = hydrated?.csrf ?? "";
  const unit = hydrated?.unit ?? "kg";
  const isIndividual = mode === "individual";
  const isGroup = mode === "group";

  // usePlanData is the sole owner of program/weeks/phases (CONTRACT.md rule 1).
  const planData = usePlanData(
    planId,
    csrf,
    {
      program: hydrated?.program ?? [],
      weeks: hydrated?.weeks ?? [],
      phases: hydrated?.phases ?? [],
      viewing: hydrated?.viewing ?? null,
      history: hydrated?.history,
    },
    { athlete: hydrated?.athlete ?? null, group: hydrated?.group ?? null },
  );

  const autosave = useAutosave({
    planId,
    csrf,
    patchExercise: planData.patchExercise,
    adoptHistory: planData.adoptHistory,
  });
  const deletes = useDeletes({
    planId,
    csrf,
    program: planData.program,
    weeks: planData.weeks,
    pendingDelete: planData.pendingDelete,
    setPendingDelete: planData.setPendingDelete,
    applyPlanData: planData.applyPlanData,
  });
  // P1 (multi-week table): a self-contained sibling data owner — the grid is
  // its own server contract (serialize_mesocycle_grid), not a slice of
  // usePlanData's program/weeks/phases. Defined here (before useUndoRedo) so
  // its undo/redo are in scope to override the keyboard shortcut below.
  const gridState = useGrid({ planId, csrf, initialGrid: hydrated?.gridData ?? null });

  const undoRedo = useUndoRedo({
    planId,
    csrf,
    viewedWeekId: planData.viewedWeekId,
    history: planData.history,
    applyPlanData: planData.applyPlanData,
    // P1: the table is a sibling data owner (gridState), not a slice of
    // planData — the global Ctrl/Cmd+Z shortcut must follow whichever view
    // is actually on screen, so route it to the grid's own undo/redo while
    // the table view is active. Falls back to planData's undo/redo
    // otherwise (unchanged behavior).
    keyboardUndo: view === "table" ? gridState.undo : undefined,
    keyboardRedo: view === "table" ? gridState.redo : undefined,
  });
  const overrideEditor = useOverrideEditor({
    planId,
    csrf,
    group: planData.group,
    adoptHistory: planData.adoptHistory,
    patchExercise: planData.patchExercise,
  });
  // P5 group: a SECOND override editor scoped to the table's data owner
  // (gridState) rather than planData — same hook, same modal, but its
  // save/clear reply repaints the grid cell (patchCellAdj) and adopts the
  // grid's own undo history. Keeps the two sibling data owners independent
  // (an adjust made in the table doesn't touch planData's one-week program,
  // and vice-versa), matching how gridState and planData already diverge.
  const gridOverrideEditor = useOverrideEditor({
    planId,
    csrf,
    group: planData.group,
    adoptHistory: gridState.adoptGridHistory,
    patchExercise: gridState.patchCellAdj,
  });
  const oneRmEditor = useOneRmEditor({
    planId,
    csrf,
    isGroup,
    adoptHistory: planData.adoptHistory,
    patchExercise: planData.patchExercise,
  });
  const reorder = useReorder({
    planId,
    csrf,
    viewedWeekId: planData.viewedWeekId,
    setPendingDelete: planData.setPendingDelete,
    program: planData.program,
    setProgram: planData.setProgram,
    applyPlanData: planData.applyPlanData,
  });
  // Issue #455 phase A2 (drag reordering): the table's own pure drag-event
  // translator, a sibling of `reorder` above — wired to gridState's two new
  // structural verbs (reorderExercises/reorderDays), not planData's. See
  // useTableReorder.ts's header for why this hook owns no state of its own.
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

  // P1: the table and the one-week view are two sibling data owners
  // (gridState vs planData) — switching the primary canvas view between them
  // must refetch whichever one is being activated, so neither view can show
  // state left stale by an edit made in the OTHER view. Never fires on
  // initial mount (only from user action, below).
  const selectView = useCallback(
    (v: ViewMode) => {
      if (v === view) return;
      if (v === "table") {
        void gridState.refetchGrid();
      } else {
        // Reactivate on the grid's current week — the week planData last
        // viewed may have been removed in the table (reloadWeek(viewedWeekId)
        // would 404 and strand a deleted week). Falls back to planData's own
        // viewed week when there's no grid.
        const target = gridState.grid
          ? gridState.grid.weeks.find((w) => w.current)?.id ?? gridState.grid.weeks[0]?.id ?? null
          : null;
        void planData.reloadWeek(target ?? undefined);
      }
      setView(v);
    },
    [view, gridState, planData],
  );

  if (!hydrated) return null;

  const flags = hydrated.flags;

  // P1: the table view can move the "current" week without planData ever
  // hearing about it ("Make current" in MesoTable calls gridState.
  // setCurrentWeek, not planData.setCurrentWeek) — so while the table is the
  // active view, Deliver must target the GRID's current week, not planData's
  // (possibly stale) viewedWeekId.
  const gridCurrentWeekId = gridState.grid
    ? gridState.grid.weeks.find((w) => w.current)?.id ?? gridState.grid.weeks[0]?.id ?? null
    : null;
  const effectiveDeliverHref =
    view === "table" && gridState.grid ? buildDeliverHref(planId, gridCurrentWeekId) : planData.deliverHref;

  return (
    <div className="meso-designer-root">
      <TopBar
        mode={mode}
        onSetMode={setMode}
        isIndividual={isIndividual}
        isGroup={isGroup}
        athlete={planData.athlete}
        group={planData.group}
        cycleLabel={planData.cycleLabel}
        onPreviewAsAthlete={() => selectView("athlete")}
        deliverHref={effectiveDeliverHref}
      />

      <div className="meso-designer-body">
        <LeftRail
          isIndividual={isIndividual}
          isGroup={isGroup}
          athlete={planData.athlete}
          group={planData.group}
          phases={planData.phases}
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
              <button type="button" className={`meso-seg-btn meso-seg-btn--v${view === "week" ? " is-on" : ""}`} onClick={() => selectView("week")}>
                This week
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
            {view === "week" && (
              <div className="meso-flex meso-canvas-autosaved">
                <div className="meso-canvas-autosaved-dot" />
                Autosaved · last edit just now
              </div>
            )}
          </div>

          <div className="meso-canvas-body">
            {view === "table" && (
              <MesoTable
                grid={gridState.grid}
                history={gridState.history}
                busy={gridState.busy}
                unit={unit}
                group={planData.group}
                onOpenOverride={(row, cell) => gridOverrideEditor.openOverride(synthesizeCellExercise(row, cell))}
                onPatchCell={gridState.patchCell}
                onRenameExercise={gridState.renameExercise}
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
                onSwapCell={gridState.swapCell}
                onFillAcrossWeeks={gridState.fillAcrossWeeks}
                onAddExerciseThisWeek={gridState.addExerciseThisWeek}
                onDragEnd={tableReorder.onDragEnd}
              />
            )}

            {view === "week" && (
              <div className="meso-week-view">
                <div className="meso-week-view-head">
                  <div>
                    <h1 className="meso-week-view-title">{planData.weekHeading}</h1>
                    <p className="meso-week-view-sub">
                      {planData.program.length + (planData.program.length === 1 ? " session" : " sessions") + " · tap any cell to edit"}
                    </p>
                  </div>
                </div>

                <WeekGrid
                  program={planData.program}
                  isGroup={isGroup}
                  unit={unit}
                  pendingDelete={planData.pendingDelete}
                  deleting={deletes.deleting}
                  onRequestRemoveDay={deletes.requestRemoveDay}
                  onConfirmPendingDelete={deletes.confirmPendingDelete}
                  onCancelPendingDelete={deletes.cancelPendingDelete}
                  onAddDay={planData.addDay}
                  onAddExercise={planData.addExercise}
                  onFieldChange={planData.updateExerciseField}
                  onCommit={(di, xi) => {
                    const ex = planData.program[di]?.exercises[xi];
                    if (ex) autosave.persistRow(ex);
                  }}
                  onRemoveExercise={deletes.removeExercise}
                  onToggleLoadType={autosave.toggleLoadType}
                  onOpenOverride={overrideEditor.openOverride}
                  onOpenOneRm={oneRmEditor.openOneRm}
                  onOneRmChange={oneRmEditor.updateValue}
                  onOneRmSave={oneRmEditor.saveOneRm}
                  onOneRmCancel={oneRmEditor.closeOneRm}
                  oneRmOpenForRow={(ex) => !!oneRmEditor.oneRm && oneRmEditor.oneRm.ex.id === ex.id}
                  oneRmEditorState={oneRmEditor.oneRm}
                  coachmarkVisible={coachmarks.coachmarkVisible}
                  dismissCoachmark={coachmarks.dismissCoachmark}
                  weeks={planData.weeks}
                  viewedWeekId={planData.viewedWeekId}
                  viewedIsCurrent={planData.viewedIsCurrent}
                  history={planData.history}
                  undoing={undoRedo.undoing}
                  onSwitchWeek={planData.switchWeek}
                  onAddWeek={planData.addWeek}
                  onMakeCurrent={planData.setCurrentWeek}
                  onRequestRemoveWeek={deletes.requestRemoveWeek}
                  onUndo={undoRedo.undo}
                  onRedo={undoRedo.redo}
                  onDragEnd={reorder.onDragEnd}
                  reordering={reorder.reordering}
                />
              </div>
            )}

            {view === "block" && (
              <BlockView
                phases={planData.phases}
                weeks={planData.weeks}
                periodStyle={periodStyle}
                onSetPeriodStyle={setPeriodStyle}
                onSwitchWeek={planData.switchWeek}
              />
            )}

            {view === "athlete" && (
              <AthletePreview
                program={planData.program}
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
        unit={unit}
        onSelectMember={overrideEditor.selectOverrideMember}
        onUpdateDraft={overrideEditor.updateDraft}
        onClose={overrideEditor.closeOverride}
        onSave={overrideEditor.saveOverride}
        onClear={overrideEditor.clearOverride}
      />

      {/* P5 group: the table view's own override modal (grid-scoped editor).
          Portal-free like its sibling above; only one is ever non-null at a
          time since each opens from its own view (WeekGrid vs MesoTable). */}
      <OverrideModal
        override={gridOverrideEditor.override}
        overrideHasExisting={gridOverrideEditor.overrideHasExisting}
        unit={unit}
        onSelectMember={gridOverrideEditor.selectOverrideMember}
        onUpdateDraft={gridOverrideEditor.updateDraft}
        onClose={gridOverrideEditor.closeOverride}
        onSave={gridOverrideEditor.saveOverride}
        onClear={gridOverrideEditor.clearOverride}
      />
    </div>
  );
}
